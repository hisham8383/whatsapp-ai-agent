"""Reminders: scheduling on create/move/cancel, sending, retries, exactly-once (§4, §5)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Booking,
    BookingStatus,
    Customer,
    Message,
    Reminder,
    SendStatus,
)
from app.kb.loader import KnowledgeBase
from app.scheduler import reminders
from app.scheduler.sending import RETRY_DELAY, ScheduledSend, apply_failed_status, drain
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import load_catalog
from tests.conftest import NOW, FakeMeta, make_conversation

MONDAY_10 = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # 10:00 Riyadh


def _booking(session: Session, start: datetime = MONDAY_10, language: str = "en") -> Booking:
    _, customer = make_conversation(session)
    customer.language = language
    booking = Booking(
        customer_id=customer.id,
        service_id="cleaning",
        staff_id="dr-sara",
        start_at=start,
        end_at=start + timedelta(minutes=45),
    )
    session.add(booking)
    session.flush()
    return booking


def _pending(session: Session) -> list[datetime]:
    rows = session.execute(
        select(Reminder.send_at)
        .where(Reminder.status == SendStatus.PENDING)
        .order_by(Reminder.send_at)
    ).scalars()
    return [r if r.tzinfo else r.replace(tzinfo=UTC) for r in rows]


def test_created_per_offset(session: Session, settings: Settings) -> None:
    booking = _booking(session)
    created = reminders.sync_for_booking(session, settings, booking, NOW)
    assert [r.send_at for r in created] == [
        MONDAY_10 - timedelta(hours=24),
        MONDAY_10 - timedelta(hours=2),
    ]
    assert {r.language for r in created} == {"en"}
    assert {r.template_name for r in created} == {"appointment_reminder"}


def test_past_offsets_skipped(session: Session, settings: Settings) -> None:
    booking = _booking(session, start=NOW + timedelta(hours=5))  # booked 5h ahead
    reminders.sync_for_booking(session, settings, booking, NOW)
    assert _pending(session) == [NOW + timedelta(hours=3)]  # only "2h before"


def test_move_replaces_and_cancel_removes(session: Session, settings: Settings) -> None:
    booking = _booking(session)
    reminders.sync_for_booking(session, settings, booking, NOW)
    booking.start_at = MONDAY_10 + timedelta(days=1)
    reminders.sync_for_booking(session, settings, booking, NOW)
    new = MONDAY_10 + timedelta(days=1)
    assert _pending(session) == [new - timedelta(hours=24), new - timedelta(hours=2)]
    booking.status = BookingStatus.CANCELLED
    reminders.sync_for_booking(session, settings, booking, NOW)
    assert _pending(session) == []
    cancelled = (
        session.execute(select(Reminder).where(Reminder.status == SendStatus.CANCELLED))
        .scalars()
        .all()
    )
    assert len(cancelled) == 4


def test_ensure_current_is_idempotent(session: Session, settings: Settings) -> None:
    booking = _booking(session)
    assert reminders.ensure_current(session, settings, booking, NOW)
    assert not reminders.ensure_current(session, settings, booking, NOW)
    booking.start_at = MONDAY_10 + timedelta(hours=3)  # staff moved it in the calendar
    assert reminders.ensure_current(session, settings, booking, NOW)
    assert _pending(session)[-1] == MONDAY_10 + timedelta(hours=1)


# ------------------------------------------------------------ sending


@pytest.fixture
def sender(
    settings: Settings, kb: KnowledgeBase, wa_client: WhatsAppClient
) -> reminders.ReminderSender:
    return reminders.ReminderSender(settings, kb, wa_client, load_catalog(settings))


def _due(factory: sessionmaker[Session], settings: Settings, language: str = "ar") -> int:
    with factory() as s:
        booking = _booking(s, language=language)
        reminders.sync_for_booking(s, settings, booking, NOW)
        s.commit()
        return booking.id


def _clock(at: datetime) -> Callable[[], datetime]:
    return lambda: at


def test_due_reminder_sent_with_buttons(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
    fake_meta: FakeMeta,
) -> None:
    _due(session_factory, settings, language="ar")
    at = MONDAY_10 - timedelta(hours=23)  # the 24h reminder is due, the 2h one isn't
    assert drain(Reminder, sender, _clock(at), session_factory) == 1
    [req] = fake_meta.requests
    template = req["template"]
    assert (template["name"], template["language"]["code"]) == ("appointment_reminder", "ar")
    params = [p["text"] for p in template["components"][0]["parameters"]]
    assert params == ["عيادة ابتسامة التجريبية", "الاثنين 5 أكتوبر", "10:00"]
    payloads = [c["parameters"][0]["payload"] for c in template["components"][1:]]
    with session_factory() as s:
        sent = s.execute(select(Reminder).where(Reminder.status == SendStatus.SENT)).scalar_one()
        assert payloads == [f"rem:{sent.id}:confirm", f"rem:{sent.id}:reschedule"]
        assert sent.wa_message_id == "wamid.OUT1" and sent.attempts == 1
        msg = s.execute(select(Message)).scalar_one()  # visible in the inbox thread
        assert msg.type == "template" and msg.body is not None and "تذكير" in msg.body


def test_sent_exactly_once_across_runners(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
    fake_meta: FakeMeta,
) -> None:
    """Two scheduler passes (e.g. an old and a new pod) never send the same row twice.

    SQLite has no row locks; the concurrent case runs on Postgres in
    scripts/smoke_postgres.py.
    """
    _due(session_factory, settings)
    at = MONDAY_10 - timedelta(hours=1)  # both due
    assert drain(Reminder, sender, _clock(at), session_factory) == 2
    assert drain(Reminder, sender, _clock(at), session_factory) == 0
    assert len(fake_meta.requests) == 2


def test_skipped_when_cancelled_or_opted_out(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
    fake_meta: FakeMeta,
) -> None:
    booking_id = _due(session_factory, settings)
    with session_factory() as s:
        booking = s.get(Booking, booking_id)
        assert booking is not None
        # Cancelled directly in the DB, bypassing sync_for_booking: the send-time check catches it.
        booking.status = BookingStatus.CANCELLED
        s.commit()
    assert drain(Reminder, sender, _clock(MONDAY_10 - timedelta(hours=1)), session_factory) == 2
    assert fake_meta.requests == []
    with session_factory() as s:
        statuses = s.execute(select(Reminder.status)).scalars().all()
        assert set(statuses) == {SendStatus.SKIPPED}


def test_opted_out_of_reminders_skipped(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
    fake_meta: FakeMeta,
) -> None:
    booking_id = _due(session_factory, settings)
    with session_factory() as s:
        booking = s.get(Booking, booking_id)
        assert booking is not None
        customer = s.get(Customer, booking.customer_id)
        assert customer is not None
        customer.opted_out_reminders = True
        s.commit()
    drain(Reminder, sender, _clock(MONDAY_10 - timedelta(hours=1)), session_factory)
    assert fake_meta.requests == []


def _failing_sender(
    settings: Settings, kb: KnowledgeBase, response: Callable[[httpx.Request], httpx.Response]
) -> reminders.ReminderSender:
    client = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(response)))
    return reminders.ReminderSender(settings, kb, client, load_catalog(settings))


def test_definite_failure_retried_once(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.http.time.sleep", lambda _s: None)
    sender = _failing_sender(settings, kb, lambda r: httpx.Response(400, json={"error": {}}))
    _due(session_factory, settings)
    at = MONDAY_10 - timedelta(hours=23)
    drain(Reminder, sender, _clock(at), session_factory)
    with session_factory() as s:
        row = s.execute(select(Reminder).order_by(Reminder.id)).scalars().first()
        assert row is not None
        assert (row.status, row.attempts, row.error_code) == (SendStatus.PENDING, 1, "400")
    drain(Reminder, sender, _clock(at + RETRY_DELAY), session_factory)
    with session_factory() as s:
        row = s.execute(select(Reminder).order_by(Reminder.id)).scalars().first()
        assert row is not None
        assert (row.status, row.attempts) == (SendStatus.FAILED, 2)


def test_ambiguous_failure_not_retried(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    sender = _failing_sender(settings, kb, timeout)
    _due(session_factory, settings)
    drain(Reminder, sender, _clock(MONDAY_10 - timedelta(hours=23)), session_factory)
    with session_factory() as s:
        row = s.execute(select(Reminder).order_by(Reminder.id)).scalars().first()
        assert row is not None
        assert (row.status, row.error_code) == (SendStatus.FAILED, "ambiguous")


def test_failed_delivery_status_retried_once(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
) -> None:
    _due(session_factory, settings)
    at = MONDAY_10 - timedelta(hours=23)
    drain(Reminder, sender, _clock(at), session_factory)
    with session_factory() as s:
        assert apply_failed_status(s, "wamid.OUT1", "131026", at)
        row = s.execute(select(Reminder).order_by(Reminder.id)).scalars().first()
        assert row is not None
        assert (row.status, row.error_code, row.wa_message_id) == (
            SendStatus.PENDING,
            "131026",
            None,
        )
        s.commit()
    drain(Reminder, sender, _clock(at + RETRY_DELAY), session_factory)
    with session_factory() as s:
        row = s.execute(select(Reminder).order_by(Reminder.id)).scalars().first()
        assert row is not None and row.status is SendStatus.SENT
        assert row.attempts == 2
        assert apply_failed_status(s, str(row.wa_message_id), "131026", at)
        final = s.execute(select(Reminder.status).where(Reminder.id == row.id)).scalar_one()
        assert final is SendStatus.FAILED  # no third try


def test_payload_parsing() -> None:
    assert reminders.parse_payload("rem:12:reschedule") == (12, "reschedule")
    assert reminders.parse_payload("rem:12:delete") is None
    assert reminders.parse_payload("slot|x") is None
    assert reminders.parse_payload(None) is None


def test_crash_after_send_is_never_retried(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
    fake_meta: FakeMeta,
) -> None:
    """A handler crash may come after Meta accepted the send: mark failed, never resend (§2)."""

    def crashing(session: Session, row: ScheduledSend, now: datetime) -> None:
        sender(session, row, now)
        raise RuntimeError("bug after the send")

    _due(session_factory, settings)
    at = MONDAY_10 - timedelta(hours=23)
    assert drain(Reminder, crashing, _clock(at), session_factory) == 1
    for later in (at, at + RETRY_DELAY, at + timedelta(hours=2)):
        drain(Reminder, sender, _clock(later), session_factory)
    first = fake_meta.requests[0]["template"]["components"][1]["parameters"][0]["payload"]
    payloads = [
        r["template"]["components"][1]["parameters"][0]["payload"] for r in fake_meta.requests
    ]
    assert payloads.count(first) == 1
    with session_factory() as s:
        statuses = s.execute(select(Reminder.status).order_by(Reminder.id)).scalars().all()
        assert statuses[0] is SendStatus.FAILED


def test_bookkeeping_failure_keeps_the_send(
    session_factory: sessionmaker[Session],
    settings: Settings,
    sender: reminders.ReminderSender,
    fake_meta: FakeMeta,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("inbox copy failed")

    monkeypatch.setattr("app.scheduler.reminders.store_template_message", broken)
    _due(session_factory, settings)
    drain(Reminder, sender, _clock(MONDAY_10 - timedelta(hours=23)), session_factory)
    with session_factory() as s:
        row = s.execute(select(Reminder).order_by(Reminder.id)).scalars().first()
        assert row is not None and row.status is SendStatus.SENT
        assert row.wa_message_id == "wamid.OUT1"


def test_offsets_inside_notice(settings: Settings, kb: KnowledgeBase) -> None:
    # Demo clinic: changes need 12h notice, so the 2h reminder can't be rescheduled.
    assert reminders.offsets_inside_notice(settings, kb) == ["2h"]


def test_reminder_sent_early_is_not_planned_again(session: Session, settings: Settings) -> None:
    """Seen live: after a reminder was sent early, a re-plan recreated it (duplicate)."""
    booking = _booking(session)
    _, second = reminders.sync_for_booking(session, settings, booking, NOW)
    second.status = SendStatus.SENT  # sent early, its time is still ahead
    session.flush()
    assert not reminders.ensure_current(session, settings, booking, NOW)
    reminders.sync_for_booking(session, settings, booking, NOW)
    assert _pending(session) == [MONDAY_10 - timedelta(hours=24)]  # not the 2h one again
