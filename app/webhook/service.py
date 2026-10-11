"""Webhook ingestion: dedupe, store, enqueue. It never calls the LLM.

CLAUDE.md §4 says to respond with 200 fast. Everything here is a few indexed
inserts inside a single transaction. The slow work (LLM, sends) happens in the
worker, which picks up the ``handle_inbound`` job enqueued below.

The message row, the dedupe claim, and the job commit together. Either the
message is stored *and* will be processed, or nothing happened and Meta's
retry gets a clean second chance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Conversation,
    ConversationState,
    Customer,
    Direction,
    Message,
    MessageStatus,
    Sender,
    utcnow,
)
from app.logging import get_logger, redact_phone
from app.scheduler.sending import apply_failed_status
from app.webhook.dedupe import claim_event
from app.webhook.parse import InboundMessage, ParsedWebhook, StatusUpdate
from app.worker import queue

log = get_logger(__name__)

# Meta status strings mapped to our enum. Unknown statuses are ignored.
_STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}
# Statuses only move forward. Meta doesn't guarantee order, and a late
# "delivered" must not overwrite "read".
_STATUS_RANK = {
    MessageStatus.PENDING: 0,
    MessageStatus.SENT: 1,
    MessageStatus.DELIVERED: 2,
    MessageStatus.READ: 3,
    MessageStatus.FAILED: 4,
}


@dataclass(slots=True)
class IngestResult:
    """Counts from one webhook delivery, for logging and tests."""

    stored: int = 0
    duplicates: int = 0
    statuses_applied: int = 0


def get_or_create_customer(session: Session, wa_id: str, name: str | None) -> Customer:
    """Find the customer by WhatsApp id, or create one.

    Only the WhatsApp id and profile name are stored (PDPL data minimization, §2).
    """
    customer = session.execute(select(Customer).where(Customer.wa_id == wa_id)).scalar_one_or_none()
    if customer is None:
        customer = Customer(wa_id=wa_id, name=name or None)
        session.add(customer)
        session.flush()
    elif name and not customer.name:
        customer.name = name
    return customer


def get_or_create_conversation(session: Session, customer: Customer) -> Conversation:
    """Return the customer's conversation. Each customer has one ongoing thread."""
    conv = session.execute(
        select(Conversation)
        .where(Conversation.customer_id == customer.id)
        .order_by(Conversation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(customer_id=customer.id)
        session.add(conv)
        session.flush()
    return conv


def _utc(value: datetime) -> datetime:
    # SQLite (tests) returns naive datetimes; everything is stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _store_inbound(session: Session, msg: InboundMessage) -> None:
    customer = get_or_create_customer(session, msg.wa_id, msg.profile_name)
    conv = get_or_create_conversation(session, customer)
    # Opens or extends the 24h customer-service window (§4). Only forward: Meta
    # redelivers failed webhooks for days, so an old message can arrive after
    # newer ones and must not shrink the window.
    last = conv.last_customer_msg_at
    if last is None or _utc(msg.timestamp) > _utc(last):
        conv.last_customer_msg_at = msg.timestamp
    if conv.state is ConversationState.HUMAN:
        conv.unread_for_staff += 1
    session.add(
        Message(
            conversation_id=conv.id,
            direction=Direction.INBOUND,
            sender=Sender.CUSTOMER,
            wa_message_id=msg.wa_message_id,
            type=msg.original_type,
            body=msg.text,
            status=MessageStatus.RECEIVED,
            created_at=msg.timestamp,
        )
    )
    queue.enqueue(
        session,
        queue.JOB_HANDLE_INBOUND,
        {
            "conversation_id": conv.id,
            "wa_message_id": msg.wa_message_id,
            "type": msg.type,
            "reply_id": msg.reply_id,
        },
    )
    log.info(
        "inbound stored",
        extra={
            "conversation_id": conv.id,
            "wa_message_id": msg.wa_message_id,
            "wa_id": redact_phone(msg.wa_id),
        },
    )


def _apply_status(session: Session, st: StatusUpdate) -> bool:
    new = _STATUS_MAP.get(st.status)
    if new is None:
        return False
    message = session.execute(
        select(Message).where(Message.wa_message_id == st.wa_message_id)
    ).scalar_one_or_none()
    if message is None:
        if new is MessageStatus.FAILED:
            # Staff alerts aren't stored as messages; they may still need their retry.
            return apply_failed_status(session, st.wa_message_id, st.error_code, utcnow())
        return False  # e.g. sent from the WhatsApp Business app, not by us
    if _STATUS_RANK.get(new, 0) < _STATUS_RANK.get(message.status, 0):
        return False
    message.status = new
    if new is MessageStatus.FAILED:
        message.error_code = st.error_code
        # A template send (reminder, follow-up) that Meta accepted and then
        # failed is retried once (§4).
        apply_failed_status(session, st.wa_message_id, st.error_code, utcnow())
        log.warning(
            "outbound message failed: %s",
            st.error_title or "unknown",
            extra={
                "conversation_id": message.conversation_id,
                "wa_message_id": st.wa_message_id,
                "error_code": st.error_code,
            },
        )
    return True


def ingest(session: Session, parsed: ParsedWebhook) -> IngestResult:
    """Persist a parsed webhook: new messages are stored and enqueued, and statuses are applied.

    Args:
        session: An open session. The caller commits.
        parsed: Output of ``parse_webhook``.

    Returns:
        Counts, for logging and tests.

    Failure modes:
        Database errors propagate. The route then returns 500, the transaction
        rolls back, and Meta retries the delivery later.
    """
    result = IngestResult()
    for msg in parsed.messages:
        if not claim_event(session, msg.wa_message_id):
            result.duplicates += 1
            log.info("duplicate delivery skipped", extra={"wa_message_id": msg.wa_message_id})
            continue
        _store_inbound(session, msg)
        result.stored += 1
    for st in parsed.statuses:
        if _apply_status(session, st):
            result.statuses_applied += 1
    return result
