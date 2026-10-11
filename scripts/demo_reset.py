"""Reset a DEMO deployment to a clean, repeatable state: ``python -m scripts.demo_reset --yes``.

Runs where the database is reachable (on Cloud Run: the ``standard-demo-reset``
job started by ``deploy/gcp/demo-start.ps1``). It:

1. deletes every customer, conversation, message and booking (plus the
   dedupe ledger and finished/pending reply jobs), so the bot remembers
   nothing from earlier takes;
2. with ``BOOKING_PROVIDER=google_calendar``: deletes the events the
   assistant created, and earlier demo blocks, in the test calendar
   (yesterday .. +30 days); events added by hand are left alone;
3. makes **next Tuesday** fully booked, so the "full day, offers other
   times" scene is reproducible: one busy event in Google Calendar, or
   bookings for a placeholder customer with the built-in provider.

Safety: it refuses to run unless ``BUSINESS_CONFIG_PATH`` points at the demo
clinic (``examples/demo-clinic``). A client deployment can never be wiped by it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta
from urllib.parse import quote

from sqlalchemy import delete

from app.booking.base import build_booking_provider
from app.booking.google_calendar import AGENT_TAG, GoogleCalendarProvider
from app.booking.internal import InternalProvider
from app.config import get_settings
from app.db.models import (
    Booking,
    Conversation,
    Customer,
    Job,
    Message,
    ProcessedEvent,
    utcnow,
)
from app.db.session import session_scope
from app.kb.loader import KnowledgeBase, load_knowledge
from app.worker.queue import JOB_HANDLE_INBOUND

DEMO_MARKER = "examples/demo-clinic"
DEMO_BLOCK_TAG = "wa_demo_block"
TUESDAY = 1  # date.weekday()


def next_tuesday(today: date) -> date:
    """The first Tuesday strictly after ``today``."""
    days = (TUESDAY - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


def wipe_database() -> dict[str, int]:
    """Delete all conversation and booking data. Returns row counts per table."""
    counts: dict[str, int] = {}
    with session_scope() as s:
        for name, stmt in (
            ("messages", delete(Message)),
            ("bookings", delete(Booking)),
            ("conversations", delete(Conversation)),
            ("customers", delete(Customer)),
            ("processed_events", delete(ProcessedEvent)),
            ("reply_jobs", delete(Job).where(Job.type == JOB_HANDLE_INBOUND)),
        ):
            counts[name] = int(s.execute(stmt).rowcount or 0)  # type: ignore[attr-defined]
    return counts


def clean_calendar(gcal: GoogleCalendarProvider, tz_today: date) -> int:
    """Delete assistant-created events and old demo blocks near today. Returns how many."""
    tz = gcal._tz
    start = datetime.combine(tz_today - timedelta(days=1), time.min, tzinfo=tz)
    end = datetime.combine(tz_today + timedelta(days=31), time.min, tzinfo=tz)
    removed = 0
    for ev in gcal._list_events(start, end):
        private = (ev.get("extendedProperties") or {}).get("private") or {}
        if ev.get("status") != "cancelled" and (
            private.get(AGENT_TAG) or private.get(DEMO_BLOCK_TAG)
        ):
            url = f"{gcal._events_url}/{quote(str(ev['id']), safe='')}"
            gcal._call("DELETE", url, ok_statuses=(404, 410))
            removed += 1
    return removed


def block_day_in_calendar(gcal: GoogleCalendarProvider, day: date) -> None:
    """One busy event over the whole day: blocks every staff member (hand-added style)."""
    tz = gcal._tz
    gcal._call(
        "POST",
        gcal._events_url,
        body={
            "summary": "Fully booked (demo)",
            "description": "Created by scripts/demo_reset.py for the 'full day' demo scene.",
            "start": {"dateTime": datetime.combine(day, time.min, tzinfo=tz).isoformat()},
            "end": {
                "dateTime": datetime.combine(
                    day + timedelta(days=1), time.min, tzinfo=tz
                ).isoformat()
            },
            "transparency": "opaque",
            "extendedProperties": {"private": {DEMO_BLOCK_TAG: "1"}},
        },
    )


def block_day_internally(provider: InternalProvider, kb: KnowledgeBase, day: date) -> int:
    """Book every free slot of every service on ``day`` for a placeholder customer."""
    now = utcnow()
    booked = 0
    with session_scope() as s:
        filler = Customer(wa_id="999000000000", name="Demo: fully booked")
        s.add(filler)
        s.flush()
        for service in kb.services:
            while slots := provider.get_availability(s, service, day, day, None, now):
                provider.create(s, filler, service, slots[0].start, None, None, now)
                booked += 1
    return booked


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really wipe the demo data")
    args = parser.parse_args()
    settings = get_settings()
    if DEMO_MARKER not in settings.business_config_path.replace("\\", "/"):
        print(f"Refusing: BUSINESS_CONFIG_PATH is not the demo clinic ({DEMO_MARKER}).")
        return 2
    if not args.yes:
        print("Dry run. Pass --yes to wipe the demo data.")
        return 2

    kb = load_knowledge(settings.business_config_path, settings.faq_path, settings.style_guide_path)
    provider = build_booking_provider(settings, kb)
    today = utcnow().astimezone(settings.tz).date()
    tuesday = next_tuesday(today)

    counts = wipe_database()
    print("database wiped:", ", ".join(f"{k} {v}" for k, v in counts.items()))
    if isinstance(provider, GoogleCalendarProvider):
        print(f"calendar: {clean_calendar(provider, today)} assistant/demo events deleted")
        block_day_in_calendar(provider, tuesday)
        print(f"calendar: {tuesday.isoformat()} (Tuesday) blocked for the full-day scene")
    elif isinstance(provider, InternalProvider):
        n = block_day_internally(provider, kb, tuesday)
        print(f"bookings: {tuesday.isoformat()} (Tuesday) filled with {n} placeholder bookings")
    print("demo reset: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
