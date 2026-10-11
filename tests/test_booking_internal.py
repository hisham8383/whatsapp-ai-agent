"""InternalProvider: availability rules, conflicts, and each adapter method (CLAUDE.md §6)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.booking.base import BookingError, check_change_allowed
from app.booking.internal import InternalProvider
from app.db.models import BookingStatus
from app.kb.loader import KnowledgeBase, Service
from tests.conftest import NOW, make_conversation

MONDAY = date(2026, 10, 5)


def _local(provider: InternalProvider, dt: datetime) -> str:
    return dt.astimezone(provider._tz).strftime("%a %H:%M")


def _svc(kb: KnowledgeBase, service_id: str) -> Service:
    svc = kb.service(service_id)
    assert svc is not None
    return svc


def test_availability_follows_hours_interval_and_lead_time(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    cleaning = _svc(kb, "cleaning")
    today = provider.get_availability(session, cleaning, NOW.date(), NOW.date(), None, NOW)
    # Now is 09:00 with a 2h lead time, so the first start is 11:00; 45 min must fit before 21:00.
    assert _local(provider, today[0].start) == "Sun 11:00"
    assert _local(provider, today[-1].start) == "Sun 20:00"
    assert all(s.staff_id == "dr-sara" for s in today)
    friday = date(2026, 10, 9)
    assert provider.get_availability(session, cleaning, friday, friday, None, NOW) == []


def test_horizon_limits_availability(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    far = NOW.date() + timedelta(days=31)
    assert provider.get_availability(session, _svc(kb, "checkup"), far, far, None, NOW) == []


def test_booking_blocks_its_slot_plus_buffer(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    _, customer = make_conversation(session)
    cleaning = _svc(kb, "cleaning")
    first = provider.get_availability(session, cleaning, MONDAY, MONDAY, None, NOW)[0]
    # 09:00-09:45 plus a 10 min buffer: the next start on the 30 min grid is 10:00.
    provider.create(session, customer, cleaning, first.start, None, None, NOW)
    after = provider.get_availability(session, cleaning, MONDAY, MONDAY, None, NOW)
    assert [_local(provider, s.start) for s in after[:2]] == ["Mon 10:00", "Mon 10:30"]
    with pytest.raises(BookingError, match="slot_unavailable"):
        provider.create(session, customer, cleaning, first.start, None, None, NOW)


def test_any_staff_uses_the_next_free_one(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    _, customer = make_conversation(session)
    checkup = _svc(kb, "checkup")
    start = provider.get_availability(session, checkup, MONDAY, MONDAY, None, NOW)[0].start
    a = provider.create(session, customer, checkup, start, None, None, NOW)
    b = provider.create(session, customer, checkup, start, None, None, NOW)
    assert {a.staff_id, b.staff_id} == {"dr-sara", "dr-noura"}
    with pytest.raises(BookingError, match="slot_unavailable"):
        provider.create(session, customer, checkup, start, None, None, NOW)


def test_staff_who_dont_offer_a_service_are_rejected(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    with pytest.raises(BookingError, match="doesn't offer"):
        provider.get_availability(session, _svc(kb, "whitening"), MONDAY, MONDAY, "dr-noura", NOW)


@pytest.mark.parametrize(
    ("hours_ahead", "code"),
    [(-1, "in_past"), (1, "too_soon"), (24 * 40, "too_far")],
)
def test_create_checks_rules(
    session: Session, provider: InternalProvider, kb: KnowledgeBase, hours_ahead: int, code: str
) -> None:
    _, customer = make_conversation(session)
    start = NOW + timedelta(hours=hours_ahead)
    with pytest.raises(BookingError, match=code):
        provider.create(session, customer, _svc(kb, "checkup"), start, None, None, NOW)


def test_create_outside_hours(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    _, customer = make_conversation(session)
    at_2030 = datetime(2026, 10, 5, 20, 30, tzinfo=provider._tz)
    with pytest.raises(BookingError, match="outside_hours"):  # 60 min would overrun 21:00
        provider.create(session, customer, _svc(kb, "whitening"), at_2030, None, None, NOW)


def test_reschedule_cancel_and_list(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    _, customer = make_conversation(session)
    cleaning = _svc(kb, "cleaning")
    slots = provider.get_availability(session, cleaning, MONDAY, MONDAY, None, NOW)
    booking = provider.create(session, customer, cleaning, slots[0].start, None, None, NOW)
    # Moving by 30 min overlaps the booking itself, which must not count as busy.
    provider.check_reschedule(session, booking, slots[1].start, NOW)
    provider.reschedule(session, booking, slots[1].start, NOW)
    assert provider.list_for_customer(session, customer, NOW)[0].start_at == slots[1].start
    provider.cancel(session, booking, "can't make it", NOW)
    assert booking.status is BookingStatus.CANCELLED
    assert provider.list_for_customer(session, customer, NOW) == []


def test_cancel_notice_policy(
    session: Session, provider: InternalProvider, kb: KnowledgeBase
) -> None:
    _, customer = make_conversation(session)
    checkup = _svc(kb, "checkup")
    today = provider.get_availability(session, checkup, NOW.date(), NOW.date(), None, NOW)
    booking = provider.create(session, customer, checkup, today[0].start, None, None, NOW)
    with pytest.raises(BookingError, match="too_late_to_change"):  # 12h notice in the demo
        check_change_allowed(kb, booking, NOW)
    check_change_allowed(kb, booking, NOW - timedelta(days=1))
