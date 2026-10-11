"""Scheduler tick: task order, isolation, booking sync cadence (CLAUDE.md §3)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.service import AlertSender, queue_handoff_alerts
from app.config import BookingProviderName, Settings
from app.conversation.tools import hand_off
from app.db.models import Booking, SendStatus, StaffAlert
from app.kb.loader import KnowledgeBase
from app.scheduler.booking_sync import BookingSync
from app.scheduler.followups import FollowUpSender
from app.scheduler.reminders import ReminderSender
from app.scheduler.runner import Scheduler
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import load_catalog
from tests.conftest import NOW, FakeMeta, make_conversation


class FakeProvider:
    """Stands in for an external booking system: reports one moved booking per sync."""

    def __init__(self) -> None:
        self.calls = 0
        self.move: Booking | None = None

    def sync_external(self, session: Session, now: datetime) -> list[Booking]:
        self.calls += 1
        if self.move is None:
            return []
        booking = session.get(Booking, self.move.id)
        assert booking is not None
        booking.start_at = booking.start_at + timedelta(hours=2)
        self.move = None
        return [booking]


def _scheduler(
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
    factory: sessionmaker[Session],
    clock: list[datetime],
    provider: FakeProvider | None = None,
) -> Scheduler:
    catalog = load_catalog(settings)
    sync = BookingSync(settings, kb, provider, wa_client, catalog) if provider else None  # type: ignore[arg-type]
    return Scheduler(
        settings,
        alerts=AlertSender(settings, wa_client),
        follow_ups=FollowUpSender(settings, kb, wa_client, catalog),
        reminders=ReminderSender(settings, kb, wa_client, catalog),
        sync=sync,
        clock=lambda: clock[0],
        factory=factory,
    )


def test_tick_sends_alerts_and_survives_a_failing_task(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as s:
        conv, _ = make_conversation(s)
        hand_off(conv, "complaint", "x")
        queue_handoff_alerts(s, settings, conv, NOW)
        s.commit()

    def broken(*_: object) -> int:
        raise RuntimeError("planner bug")

    monkeypatch.setattr("app.scheduler.runner.followups.plan_due", broken)
    _scheduler(settings, kb, wa_client, session_factory, [NOW]).tick()
    assert [r["template"]["name"] for r in fake_meta.requests] == ["staff_handoff_alert"]
    with session_factory() as s:
        assert s.execute(select(StaffAlert.status)).scalar_one() is SendStatus.SENT


def test_booking_sync_cadence_and_update_message(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "booking_provider", BookingProviderName.GOOGLE_CALENDAR)
    provider = FakeProvider()
    with session_factory() as s:
        _, customer = make_conversation(s)
        start = NOW + timedelta(days=3)
        booking = Booking(
            customer_id=customer.id, service_id="cleaning", start_at=start, end_at=start
        )
        s.add(booking)
        s.commit()
        provider.move = booking
    clock = [NOW]
    scheduler = _scheduler(settings, kb, wa_client, session_factory, clock, provider)
    scheduler.tick()
    scheduler.tick()  # within BOOKING_SYNC_MINUTES: no second sync
    assert provider.calls == 1
    [req] = fake_meta.requests
    assert req["template"]["name"] == "booking_confirmation_update"
    clock[0] = NOW + timedelta(minutes=settings.booking_sync_minutes)
    scheduler.tick()
    assert provider.calls == 2


def test_internal_provider_never_syncs(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
) -> None:
    provider = FakeProvider()
    _scheduler(settings, kb, wa_client, session_factory, [NOW], provider).tick()
    assert provider.calls == 0
