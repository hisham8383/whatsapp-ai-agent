"""Daily retention task (CLAUDE.md §7, PDPL)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AlertChannel,
    Booking,
    Conversation,
    Customer,
    Direction,
    Message,
    MessageStatus,
    ProcessedEvent,
    Reminder,
    Sender,
    SendStatus,
    StaffAlert,
)
from app.scheduler.retention import run_retention
from tests.conftest import make_conversation

NOW = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)


def _message(conv_id: int, body: str, age_days: int) -> Message:
    return Message(
        conversation_id=conv_id,
        direction=Direction.INBOUND,
        sender=Sender.CUSTOMER,
        type="text",
        body=body,
        status=MessageStatus.RECEIVED,
        created_at=NOW - timedelta(days=age_days),
    )


def test_old_text_is_blanked_and_old_events_deleted(session: Session) -> None:
    conv, _ = make_conversation(session)
    conv.handoff_summary = "old summary"
    conv.updated_at = NOW - timedelta(days=100)
    session.add_all([_message(conv.id, "old", 91), _message(conv.id, "recent", 10)])
    session.add_all(
        [
            ProcessedEvent(wa_message_id="old", processed_at=NOW - timedelta(days=8)),
            ProcessedEvent(wa_message_id="new", processed_at=NOW - timedelta(days=1)),
        ]
    )
    session.flush()
    result = run_retention(session, 90, now=NOW)
    assert (result.messages_blanked, result.summaries_blanked, result.events_deleted) == (1, 1, 1)
    bodies = session.execute(select(Message.body).order_by(Message.id)).scalars().all()
    assert bodies == [None, "recent"]
    assert session.execute(select(ProcessedEvent.wa_message_id)).scalars().all() == ["new"]
    session.refresh(conv)
    assert session.get(Conversation, conv.id).handoff_summary is None  # type: ignore[union-attr]


def test_finished_scheduled_sends_deleted_after_90_days(session: Session) -> None:
    conv, customer = make_conversation(session)
    booking = Booking(
        customer_id=customer.id,
        service_id="cleaning",
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
    )
    session.add(booking)
    session.flush()

    def reminder(status: SendStatus, age: int) -> Reminder:
        return Reminder(
            booking_id=booking.id,
            send_at=NOW,
            template_name="t",
            language="ar",
            status=status,
            created_at=NOW - timedelta(days=age),
        )

    session.add_all(
        [
            reminder(SendStatus.SENT, 91),  # deleted
            reminder(SendStatus.PENDING, 91),  # kept: not finished
            reminder(SendStatus.SENT, 10),  # kept: recent
            StaffAlert(
                conversation_id=conv.id,
                contact="966500000900",
                channel=AlertChannel.WHATSAPP,
                status=SendStatus.SENT,
                send_at=NOW,
                created_at=NOW - timedelta(days=100),
            ),
        ]
    )
    session.flush()
    result = run_retention(session, 90, now=NOW)
    assert result.sends_deleted == 2
    left = session.execute(select(Reminder.status).order_by(Reminder.id)).scalars().all()
    assert left == [SendStatus.PENDING, SendStatus.SENT]
    assert session.execute(select(StaffAlert)).first() is None
    assert session.get(Customer, customer.id) is not None  # opt-out flags must outlive retention
