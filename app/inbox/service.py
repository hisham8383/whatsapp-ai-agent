"""Inbox operations. The routes stay thin; all logic lives here (CLAUDE.md §9).

Staff replies go through the same ``WhatsAppClient`` as the bot, so the
24-hour window guard applies to them too (§4). Once the window has closed the
page swaps the reply box for **Re-open with template** (``reopen``), which
sends the approved ``TEMPLATE_REOPEN`` template; ``staff_reply`` refuses a
free-form send anyway if a stale page tries one.

Staff also see, per booking, the state of its reminders, and per customer,
their follow-up and opt-out state (§5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.booking.base import as_utc
from app.config import Settings
from app.conversation.outbound import store_template_message
from app.db.models import (
    Booking,
    BookingStatus,
    Conversation,
    ConversationState,
    Customer,
    Direction,
    FollowUp,
    Message,
    MessageStatus,
    Reminder,
    Sender,
    SendStatus,
    utcnow,
)
from app.kb.loader import KnowledgeBase
from app.logging import get_logger
from app.scheduler import reminders
from app.scheduler.followups import first_name
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import TemplateCatalog
from app.whatsapp.window import is_window_open

log = get_logger(__name__)

WINDOW_CLOSED_TEXT = (
    "The 24-hour window has closed, so WhatsApp only allows an approved template. "
    "Re-open the conversation below; you can reply freely once the customer answers."
)
MAX_REPLY_CHARS = 4096  # WhatsApp text limit
DUPLICATE_WINDOW = timedelta(seconds=30)  # identical staff replies inside this are ignored
THREAD_MESSAGES = 200


class NotFoundError(LookupError):
    """No conversation with that id."""


@dataclass(frozen=True, slots=True)
class HandoffRow:
    conversation_id: int
    customer_name: str | None
    wa_id: str
    reason: str | None
    summary: str | None
    unread: int
    handed_off_at: datetime | None  # local time
    window_open: bool


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    sender: str  # customer | bot | staff
    body: str | None  # None once retention removed it
    at: datetime  # local time
    status: str


@dataclass(frozen=True, slots=True)
class Thread:
    conversation_id: int
    customer_name: str | None
    wa_id: str
    state: str
    reason: str | None
    summary: str | None
    window_open: bool
    messages: list[ThreadMessage]
    reopen_waiting: bool = False  # a re-open template was sent and the customer hasn't replied
    opted_out_followups: bool = False
    opted_out_reminders: bool = False
    follow_up: str | None = None  # latest lead follow-up, e.g. "sent Mon 05 Oct, 10:00"


@dataclass(frozen=True, slots=True)
class BookingRow:
    start: datetime  # local time
    service: str
    staff: str | None
    customer_name: str | None
    wa_id: str
    reminders: str = ""  # e.g. "24h before: sent · 2h before: due Mon 05 Oct, 08:30"


def handed_off(session: Session, tz: ZoneInfo) -> list[HandoffRow]:
    """Conversations waiting for staff, most recent customer message first."""
    rows = session.execute(
        select(Conversation, Customer)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.state == ConversationState.HUMAN)
        .order_by(Conversation.last_customer_msg_at.desc().nulls_last(), Conversation.id.desc())
    ).all()
    return [
        HandoffRow(
            conversation_id=conv.id,
            customer_name=cust.name,
            wa_id=cust.wa_id,
            reason=conv.handoff_reason,
            summary=conv.handoff_summary,
            unread=conv.unread_for_staff,
            handed_off_at=as_utc(conv.handed_off_at).astimezone(tz) if conv.handed_off_at else None,
            window_open=is_window_open(conv.last_customer_msg_at),
        )
        for conv, cust in rows
    ]


def unread_total(session: Session) -> int:
    """Unread customer messages across handed-off conversations, for the page title."""
    total = session.execute(
        select(func.coalesce(func.sum(Conversation.unread_for_staff), 0)).where(
            Conversation.state == ConversationState.HUMAN
        )
    ).scalar_one()
    return int(total)


def _conversation(session: Session, conversation_id: int) -> tuple[Conversation, Customer]:
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        raise NotFoundError(conversation_id)
    customer = session.get(Customer, conv.customer_id)
    if customer is None:
        raise NotFoundError(conversation_id)
    return conv, customer


def open_thread(session: Session, conversation_id: int, tz: ZoneInfo) -> Thread:
    """Load a conversation for staff and mark its messages as read.

    Raises:
        NotFoundError: No such conversation.
    """
    conv, customer = _conversation(session, conversation_id)
    conv.unread_for_staff = 0
    msgs = list(
        session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(THREAD_MESSAGES)
        ).scalars()
    )
    latest_followup = session.execute(
        select(FollowUp)
        .where(FollowUp.customer_id == customer.id)
        .order_by(FollowUp.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return Thread(
        conversation_id=conv.id,
        customer_name=customer.name,
        wa_id=customer.wa_id,
        state=conv.state.value,
        reason=conv.handoff_reason,
        summary=conv.handoff_summary,
        window_open=is_window_open(conv.last_customer_msg_at),
        reopen_waiting=_reopen_waiting(session, conv),
        opted_out_followups=customer.opted_out_followups,
        opted_out_reminders=customer.opted_out_reminders,
        follow_up=_send_state(latest_followup, tz) if latest_followup else None,
        messages=[
            ThreadMessage(
                sender=m.sender.value,
                body=m.body,
                at=as_utc(m.created_at).astimezone(tz),
                status=m.status.value,
            )
            for m in reversed(msgs)
        ],
    )


def staff_reply(session: Session, client: WhatsAppClient, conversation_id: int, text: str) -> bool:
    """Send a staff message to the customer and store it.

    The same text sent to the same chat again within ``DUPLICATE_WINDOW`` (a
    double click, a browser resubmit) is ignored, so the customer never gets
    it twice.

    Returns:
        True if the message was sent, False if it was an ignored duplicate.

    Raises:
        NotFoundError: No such conversation.
        ValueError: Empty or over-long text.
        app.whatsapp.window.OutsideServiceWindowError: The 24h window is closed (§4).
        app.http.ExternalServiceError: Meta rejected the send.
    """
    body = text.strip()
    if not body:
        raise ValueError("empty reply")
    if len(body) > MAX_REPLY_CHARS:
        raise ValueError("reply too long")
    conv, customer = _conversation(session, conversation_id)
    if _just_sent(session, conv.id, body):
        # A double click or a resubmitted form: the customer already has this message.
        log.info("duplicate staff reply ignored", extra={"conversation_id": conv.id})
        return False
    wamid = client.send_text(customer.wa_id, body, last_customer_msg_at=conv.last_customer_msg_at)
    session.add(
        Message(
            conversation_id=conv.id,
            direction=Direction.OUTBOUND,
            sender=Sender.STAFF,
            wa_message_id=wamid,
            type="text",
            body=body,
            status=MessageStatus.PENDING,
        )
    )
    log.info("staff reply sent", extra={"conversation_id": conv.id})
    return True


def _just_sent(session: Session, conversation_id: int, body: str) -> bool:
    """True if staff sent exactly this text to this chat within ``DUPLICATE_WINDOW``."""
    last = session.execute(
        select(Message.body, Message.created_at)
        .where(Message.conversation_id == conversation_id, Message.sender == Sender.STAFF)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    ).first()
    return (
        last is not None
        and last.body == body
        and utcnow() - as_utc(last.created_at) < DUPLICATE_WINDOW
    )


def _reopen_waiting(session: Session, conv: Conversation) -> bool:
    """True if staff re-opened the chat and the customer hasn't written since."""
    last = session.execute(
        select(Message.direction, Message.sender, Message.type)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
    ).first()
    return (
        last is not None
        and last.direction is Direction.OUTBOUND
        and last.sender is Sender.STAFF
        and last.type == "template"
    )


def reopen(
    session: Session,
    client: WhatsAppClient,
    catalog: TemplateCatalog,
    settings: Settings,
    kb: KnowledgeBase,
    conversation_id: int,
) -> bool:
    """Send the ``TEMPLATE_REOPEN`` template so the customer can answer (§4).

    Only when the 24h window is closed (inside it, staff just reply), and only
    once until the customer answers: repeating an unanswered template is how
    numbers get reported as spam.

    Returns:
        True if sent; False if the window is open or a re-open is already waiting.

    Raises:
        NotFoundError: No such conversation.
        app.http.ExternalServiceError: Meta rejected the send.
    """
    conv, customer = _conversation(session, conversation_id)
    if is_window_open(conv.last_customer_msg_at) or _reopen_waiting(session, conv):
        return False
    lang = customer.language if customer.language in ("ar", "en") else settings.default_language
    business = kb.business
    params = [
        first_name(customer.name, lang),
        business.name_ar if lang == "ar" else business.name_en,
    ]
    name = settings.template_reopen
    wamid = client.send_template(customer.wa_id, name, lang, params)
    store_template_message(
        session, customer, catalog.render(name, lang, params), wamid, sender=Sender.STAFF
    )
    log.info("conversation re-opened with template", extra={"conversation_id": conv.id})
    return True


def _send_state(row: Reminder | FollowUp, tz: ZoneInfo) -> str:
    """``sent Mon 05 Oct, 10:00`` / ``due …`` / ``failed (131026)`` for staff."""
    fmt = "%a %d %b, %H:%M"
    if row.status is SendStatus.SENT and row.sent_at:
        return f"sent {as_utc(row.sent_at).astimezone(tz).strftime(fmt)}"
    if row.status is SendStatus.PENDING:
        return f"due {as_utc(row.send_at).astimezone(tz).strftime(fmt)}"
    if row.status is SendStatus.FAILED:
        return f"failed ({row.error_code or '?'})"
    return row.status.value


def return_to_bot(session: Session, conversation_id: int) -> None:
    """Hand the conversation back to the assistant (the "Return to bot" button, §5).

    Raises:
        NotFoundError: No such conversation.
    """
    conv, _ = _conversation(session, conversation_id)
    conv.state = ConversationState.BOT
    conv.handed_off_at = None
    conv.handoff_reason = None
    conv.handoff_summary = None
    conv.unread_for_staff = 0
    conv.pending_action = None
    conv.bot_failures = 0
    log.info("returned to bot", extra={"conversation_id": conv.id})


def upcoming_bookings(
    session: Session, kb: KnowledgeBase, tz: ZoneInfo, limit: int = 50
) -> list[BookingRow]:
    """Confirmed bookings that haven't started, earliest first (read-only list for staff)."""
    rows = session.execute(
        select(Booking, Customer)
        .join(Customer, Customer.id == Booking.customer_id)
        .where(Booking.status == BookingStatus.CONFIRMED, Booking.start_at > utcnow())
        .order_by(Booking.start_at)
        .limit(limit)
    ).all()
    planned = reminders.upcoming_for(session, [b.id for b, _ in rows])
    result = []
    for b, cust in rows:
        start = as_utc(b.start_at)
        reminder_text = " · ".join(
            f"{_hours_before(start, as_utc(r.send_at))}: {_send_state(r, tz)}"
            for r in planned.get(b.id, [])
        )
        if cust.opted_out_reminders:
            reminder_text = "customer turned reminders off"
        svc = kb.service(b.service_id)
        staff = kb.staff_member(b.staff_id) if b.staff_id else None
        result.append(
            BookingRow(
                start=as_utc(b.start_at).astimezone(tz),
                service=f"{svc.name_en} / {svc.name_ar}" if svc else b.service_id,
                staff=f"{staff.name_en} / {staff.name_ar}" if staff else None,
                customer_name=cust.name,
                wa_id=cust.wa_id,
                reminders=reminder_text or "—",
            )
        )
    return result


def _hours_before(start: datetime, send_at: datetime) -> str:
    """``24h before`` / ``90m before``: which offset a reminder is."""
    minutes = round((start - send_at).total_seconds() / 60)
    if minutes % 60 == 0:
        return f"{minutes // 60}h before"
    return f"{minutes}m before"
