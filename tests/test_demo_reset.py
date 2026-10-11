"""scripts/demo_reset.py: date logic, the demo-only safety check, and the wipe."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Booking, Customer
from app.kb.loader import KnowledgeBase
from scripts import demo_reset
from tests.conftest import NOW, make_conversation


@pytest.mark.parametrize(
    ("today", "tuesday"),
    [
        (date(2026, 9, 28), date(2026, 9, 29)),  # Monday -> tomorrow
        (date(2026, 9, 29), date(2026, 10, 6)),  # Tuesday -> next week, never today
        (date(2026, 10, 4), date(2026, 10, 6)),  # Sunday
    ],
)
def test_next_tuesday(today: date, tuesday: date) -> None:
    assert demo_reset.next_tuesday(today) == tuesday


def test_refuses_on_a_client_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "business_config_path", "./knowledge/business.yaml")
    monkeypatch.setattr("sys.argv", ["demo_reset", "--yes"])
    assert demo_reset.main() == 2


def test_wipe_and_internal_full_day(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, kb: KnowledgeBase
) -> None:
    from app.booking.internal import InternalProvider
    from app.db import session as db_session

    monkeypatch.setattr(db_session, "get_sessionmaker", lambda: session_factory)
    with session_factory() as s:
        make_conversation(s)
        s.commit()
    counts = demo_reset.wipe_database()
    assert counts["customers"] == 1 and counts["conversations"] == 1
    settings = get_settings()
    provider = InternalProvider(kb, settings.tz)
    # A future Tuesday: past days have no bookable slots.
    day = demo_reset.next_tuesday(date.today())
    booked = demo_reset.block_day_internally(provider, kb, day)
    assert booked > 0
    with session_factory() as s:
        day_free = provider.get_availability(
            s,
            kb.services[0],
            day,
            day,
            None,
            NOW,
        )
        assert day_free == []
        assert s.execute(select(Customer.name)).scalar_one() == "Demo: fully booked"
        assert len(s.execute(select(Booking)).scalars().all()) == booked
