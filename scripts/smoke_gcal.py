"""Live smoke test on a real Google Calendar: ``python -m scripts.smoke_gcal``.

Needs ``BOOKING_PROVIDER=google_calendar``, ``CALENDAR_ID`` and
``CALENDAR_CREDENTIALS`` (the key JSON, or locally a path to the key file) in
``.env``. Use a **test calendar** shared with the service account's email
("Make changes to events"), never a client's live calendar.

It runs every adapter method once against the real API, in order:
availability -> create -> list -> reschedule -> list -> cancel -> list, and
checks each result against what's in Google Calendar. The mirror rows live in
a throwaway in-memory database, so Postgres is untouched. The event it creates
is always deleted at the end, even if a step fails.
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.booking.base import as_utc, build_booking_provider
from app.booking.google_calendar import GoogleCalendarProvider
from app.config import BookingProviderName, get_settings
from app.conversation.timefmt import for_model
from app.db.models import Base, Booking, BookingStatus, Customer
from app.kb.loader import load_knowledge

results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")


def main() -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    settings = get_settings()
    if settings.booking_provider is not BookingProviderName.GOOGLE_CALENDAR:
        print("Set BOOKING_PROVIDER=google_calendar (plus CALENDAR_ID, CALENDAR_CREDENTIALS).")
        return 2
    kb = load_knowledge(settings.business_config_path, settings.faq_path, settings.style_guide_path)
    provider = build_booking_provider(settings, kb)
    assert isinstance(provider, GoogleCalendarProvider)
    tz = settings.tz

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    customer = Customer(wa_id="999000000001", name="Smoke test")
    session.add(customer)
    session.flush()

    now = datetime.now(UTC)
    service = kb.services[0]
    booking: Booking | None = None
    try:
        day = (now + timedelta(days=1)).astimezone(tz).date()
        slots = provider.get_availability(session, service, day, day + timedelta(days=6), None, now)
        check("availability from the real calendar", len(slots) >= 2, f"{len(slots)} free slots")
        if len(slots) < 2:
            return 1
        booking = provider.create(session, customer, service, slots[0].start, None, None, now)
        check(
            "create -> event in Google Calendar",
            bool(booking.external_id),
            f"{service.id} {for_model(slots[0].start, tz)} event {booking.external_id}",
        )
        again = provider.get_availability(session, service, day, day + timedelta(days=6), None, now)
        taken = [s for s in again if s.start == slots[0].start and s.staff_id == booking.staff_id]
        check("the booked slot is no longer free for that staff member", not taken)

        listed = provider.list_for_customer(session, customer, now)
        check("list finds the booking", [b.id for b in listed] == [booking.id])

        target = next(s.start for s in again if s.start > slots[0].start + timedelta(hours=2))
        provider.check_reschedule(session, booking, target, now)
        provider.reschedule(session, booking, target, now)
        listed = provider.list_for_customer(session, customer, now)
        check(
            "reschedule -> event moved",
            len(listed) == 1 and as_utc(listed[0].start_at) == target,
            for_model(target, tz),
        )

        provider.cancel(session, booking, None, now)
        check("cancel -> event deleted", booking.status is BookingStatus.CANCELLED)
        check(
            "list is empty after cancel", provider.list_for_customer(session, customer, now) == []
        )
    finally:
        if booking is not None and booking.status is BookingStatus.CONFIRMED:
            provider.cancel(session, booking, None, now)  # never leave a test event behind
            print("cleanup: test event deleted")
    failed = [n for n, ok in results if not ok]
    print("RESULT:", "PASS" if not failed else f"FAIL ({len(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
