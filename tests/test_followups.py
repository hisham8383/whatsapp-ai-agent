"""Lead follow-ups: delay, one per lead, cooldown, opt-out, booked, quiet hours (CLAUDE.md §5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Booking,
    ConversationState,
    Customer,
    FollowUp,
    LeadInterest,
    SendStatus,
)
from app.kb.loader import KnowledgeBase
from app.scheduler import followups
from app.scheduler.sending import drain
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import load_catalog
from tests.conftest import NOW, FakeMeta, make_conversation

# 20h after NOW (Sun 09:00 Riyadh) is Mon 05:00 Riyadh: inside quiet hours.
LATER = NOW + timedelta(hours=21)


def _interest(session: Session, service: str = "whitening") -> Customer:
    _, customer = make_conversation(session, name="Noura Al-Harbi")
    customer.language = "ar"
    followups.record_interest(session, customer, service, NOW)
    session.flush()
    return customer


def _queued(session: Session) -> list[FollowUp]:
    return list(session.execute(select(FollowUp)).scalars())


def test_not_before_delay(session: Session, settings: Settings) -> None:
    _interest(session)
    assert followups.plan_due(session, settings, NOW + timedelta(hours=19)) == 0
    assert session.execute(select(LeadInterest.handled_at)).scalar_one() is None


def test_queued_after_delay_and_shifted_out_of_quiet_hours(
    session: Session, settings: Settings
) -> None:
    _interest(session)
    at = NOW + timedelta(hours=20)  # Mon 05:00 local
    assert followups.plan_due(session, settings, at) == 1
    [row] = _queued(session)
    send_at = row.send_at if row.send_at.tzinfo else row.send_at.replace(tzinfo=UTC)
    assert send_at == datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # Mon 09:00 local
    assert (row.language, row.service_id, row.template_name) == ("ar", "whitening", "lead_followup")
    assert followups.plan_due(session, settings, at + timedelta(hours=1)) == 0  # one per lead


def test_duplicate_interest_recorded_once(session: Session) -> None:
    customer = _interest(session)
    assert followups.record_interest(session, customer, "whitening", NOW) is None
    assert len(session.execute(select(LeadInterest)).scalars().all()) == 1


def test_skip_rules(session: Session, settings: Settings) -> None:
    opted = _interest(session)
    opted.opted_out_followups = True

    _, booked = make_conversation(session, wa_id="966500000002")
    followups.record_interest(session, booked, "whitening", NOW)
    session.add(
        Booking(
            customer_id=booked.id,
            service_id="whitening",
            start_at=NOW + timedelta(days=3),
            end_at=NOW + timedelta(days=3, hours=1),
            created_at=NOW + timedelta(hours=1),
        )
    )

    human_conv, human = make_conversation(session, wa_id="966500000003")
    human_conv.state = ConversationState.HUMAN
    followups.record_interest(session, human, "whitening", NOW)
    session.flush()

    assert followups.plan_due(session, settings, LATER) == 0
    handled = session.execute(select(LeadInterest.handled_at)).scalars().all()
    assert all(h is not None for h in handled)  # each decided once, never again


def test_cooldown_one_per_customer(session: Session, settings: Settings) -> None:
    customer = _interest(session)
    followups.record_interest(session, customer, "cleaning", NOW)
    session.flush()
    assert followups.plan_due(session, settings, LATER) == 1  # not two
    followups.record_interest(session, customer, "checkup", LATER)
    session.flush()
    later = LATER + timedelta(days=settings.followup_cooldown_days - 1)
    assert followups.plan_due(session, settings, later) == 0


def test_sender(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    with session_factory() as s:
        _interest(s)
        followups.plan_due(s, settings, LATER)
        s.commit()
    sender = followups.FollowUpSender(settings, kb, wa_client, load_catalog(settings))
    assert drain(FollowUp, sender, lambda: LATER + timedelta(hours=4), session_factory) == 1
    template = fake_meta.requests[0]["template"]
    params = [p["text"] for p in template["components"][0]["parameters"]]
    assert template["name"] == "lead_followup"
    assert params == ["Noura", "تبييض الأسنان", "عيادة ابتسامة التجريبية"]


def test_sender_skips_customer_who_booked_meanwhile(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    with session_factory() as s:
        customer = _interest(s)
        followups.plan_due(s, settings, LATER)
        s.add(
            Booking(
                customer_id=customer.id,
                service_id="whitening",
                start_at=LATER + timedelta(days=2),
                end_at=LATER + timedelta(days=2, hours=1),
                created_at=LATER + timedelta(minutes=30),
            )
        )
        s.commit()
    sender = followups.FollowUpSender(settings, kb, wa_client, load_catalog(settings))
    drain(FollowUp, sender, lambda: LATER + timedelta(hours=4), session_factory)
    assert fake_meta.requests == []
    with session_factory() as s:
        assert s.execute(select(FollowUp.status)).scalar_one() is SendStatus.SKIPPED


def test_first_name() -> None:
    assert followups.first_name("Noura Al-Harbi", "en") == "Noura"
    assert followups.first_name(None, "ar") == "عميلنا العزيز"
    assert followups.first_name("  ", "en") == "there"


def test_booking_of_another_service_does_not_block(session: Session, settings: Settings) -> None:
    """Decided in live testing: a cleaning booked tomorrow doesn't cancel the one follow-up
    about the kids check-up the customer asked about."""
    customer = _interest(session, service="kids-checkup")
    session.add(
        Booking(
            customer_id=customer.id,
            service_id="cleaning",
            start_at=NOW + timedelta(days=2),
            end_at=NOW + timedelta(days=2, hours=1),
            created_at=NOW + timedelta(hours=1),
        )
    )
    session.flush()
    assert followups.plan_due(session, settings, LATER) == 1
    assert _queued(session)[0].service_id == "kids-checkup"
