"""Record template messages in the customer's conversation.

Template sends (reminders, follow-ups, re-open, booking updates) happen
outside the worker's reply path, but staff must still see them in the inbox
thread, and status webhooks must find them by ``wa_message_id`` (§4). This
stores them like any other outbound bot message, with ``type="template"``
and the rendered body.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Conversation,
    Customer,
    Direction,
    Message,
    MessageStatus,
    Sender,
)


def conversation_for(session: Session, customer: Customer) -> Conversation:
    """The customer's conversation, created if they never wrote (e.g. a synced booking).

    Locks the customer row first, so two schedulers sending to the same new
    customer at once can't each create a conversation (found by the Postgres
    two-runner smoke test).
    """
    session.execute(select(Customer.id).where(Customer.id == customer.id).with_for_update())
    conv = session.execute(
        select(Conversation)
        .where(Conversation.customer_id == customer.id)
        .order_by(Conversation.id)
        .limit(1)
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(customer_id=customer.id)
        session.add(conv)
        session.flush()
    return conv


def store_template_message(
    session: Session,
    customer: Customer,
    body: str,
    wa_message_id: str,
    sender: Sender = Sender.BOT,
) -> Message:
    """Add the outbound template message to the customer's conversation.

    Args:
        session: Open session; the caller commits.
        customer: Recipient.
        body: Rendered template text (``TemplateCatalog.render``).
        wa_message_id: Meta's id from ``send_template``.
        sender: ``BOT`` for scheduler sends, ``STAFF`` for inbox sends.
    """
    conv = conversation_for(session, customer)
    message = Message(
        conversation_id=conv.id,
        direction=Direction.OUTBOUND,
        sender=sender,
        wa_message_id=wa_message_id,
        type="template",
        body=body,
        status=MessageStatus.PENDING,
    )
    session.add(message)
    return message
