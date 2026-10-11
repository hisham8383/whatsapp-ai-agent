"""GoogleCalendarProvider against an in-memory fake of the Calendar API (mocked HTTP, CLAUDE.md §9).

The fake implements the token endpoint and events list / insert / get / patch /
delete. The service-account key is a real RSA key generated for the test, so
google-auth signs a real JWT; nothing leaves the process.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.booking.base import BookingError, build_booking_provider
from app.booking.google_calendar import (
    CalendarConfigError,
    GoogleCalendarProvider,
    load_credentials,
)
from app.config import BookingProviderName, Settings
from app.db.models import BookingSource, BookingStatus, Customer
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase, Service, load_knowledge
from tests.conftest import NOW, make_conversation

CAL = "clinic@group.calendar.google.com"
MONDAY = date(2026, 10, 5)
TOKEN_URI = "https://oauth2.googleapis.com/token"


@pytest.fixture(scope="module")
def sa_key_json() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "test",
            "private_key_id": "k1",
            "private_key": pem,
            "client_email": "agent@test.iam.gserviceaccount.com",
            "client_id": "1",
            "token_uri": TOKEN_URI,
        }
    )


class FakeCalendar:
    """Just enough of Calendar API v3 for the adapter."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_requests = 0
        self.down = False
        self._ids = itertools.count(1)

    def add(self, start: str, end: str, **extra: Any) -> str:
        """Add an event by hand, as staff would in Google Calendar (times: local +03:00)."""
        eid = f"manual{next(self._ids)}"
        self.events[eid] = {
            "id": eid,
            "status": "confirmed",
            "start": {"dateTime": f"{start}:00+03:00"} if "T" in start else {"date": start},
            "end": {"dateTime": f"{end}:00+03:00"} if "T" in end else {"date": end},
            **extra,
        }
        return eid

    @staticmethod
    def _t(value: dict[str, str]) -> datetime:
        if "dateTime" in value:
            return datetime.fromisoformat(value["dateTime"]).astimezone(UTC)
        return datetime.fromisoformat(value["date"]).replace(tzinfo=UTC)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if url.startswith(TOKEN_URI):
            self.token_requests += 1
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"}
            )
        assert request.headers["Authorization"] == "Bearer tok"
        if self.down:
            return httpx.Response(503, json={"error": "backendError"})
        path = unquote(request.url.path)
        assert path.startswith(f"/calendar/v3/calendars/{CAL}/events")
        tail = path.removeprefix(f"/calendar/v3/calendars/{CAL}/events").strip("/")
        if not tail and request.method == "GET":
            lo = datetime.fromisoformat(request.url.params["timeMin"])
            hi = datetime.fromisoformat(request.url.params["timeMax"])
            items = [
                e
                for e in self.events.values()
                if self._t(e["start"]) < hi and self._t(e["end"]) > lo
            ]
            return httpx.Response(200, json={"items": items})
        if not tail and request.method == "POST":
            body = json.loads(request.content)
            eid = f"ev{next(self._ids)}"
            self.events[eid] = {"id": eid, "status": "confirmed", **body}
            return httpx.Response(200, json=self.events[eid])
        event = self.events.get(tail)
        if event is None:
            return httpx.Response(404, json={"error": "notFound"})
        if request.method == "GET":
            return httpx.Response(200, json=event)
        if request.method == "PATCH":
            event.update(json.loads(request.content))
            return httpx.Response(200, json=event)
        if request.method == "DELETE":
            del self.events[tail]
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def fake() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
def gcal(
    kb: KnowledgeBase, settings: Settings, fake: FakeCalendar, sa_key_json: str
) -> GoogleCalendarProvider:
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return GoogleCalendarProvider(kb, settings.tz, CAL, load_credentials(sa_key_json), http=http)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.http.time.sleep", lambda _s: None)


def _svc(kb: KnowledgeBase, service_id: str) -> Service:
    svc = kb.service(service_id)
    assert svc is not None
    return svc


def _local(gcal: GoogleCalendarProvider, dt: datetime) -> str:
    return dt.astimezone(gcal._tz).strftime("%H:%M")


# ------------------------------------------------------------------ config


def test_key_can_be_a_file_path_locally(sa_key_json: str, tmp_path: Path) -> None:
    key_file = tmp_path / "key.json"
    key_file.write_text(sa_key_json, encoding="utf-8")
    assert load_credentials(str(key_file)).service_account_email == (
        "agent@test.iam.gserviceaccount.com"
    )


def test_bad_or_missing_config_fails_at_startup(settings: Settings, sa_key_json: str) -> None:
    with pytest.raises(CalendarConfigError, match="JSON key"):
        load_credentials("{}")
    with pytest.raises(CalendarConfigError, match="JSON key"):
        load_credentials("not json")
    s = settings.model_copy(
        update={
            "booking_provider": BookingProviderName.GOOGLE_CALENDAR,
            "calendar_id": "",
            "calendar_credentials": SecretStr(sa_key_json),
        }
    )
    kb_ = load_knowledge(s.business_config_path, s.faq_path, s.style_guide_path)
    with pytest.raises(CalendarConfigError, match="CALENDAR_ID"):
        build_booking_provider(s, kb_)
    ok = s.model_copy(update={"calendar_id": CAL})
    assert isinstance(build_booking_provider(ok, kb_), GoogleCalendarProvider)


# ------------------------------------------------------------------ availability


def test_manual_events_block_everyone_ours_block_their_staff(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    checkup = _svc(kb, "checkup")
    # Staff meeting 09:00-09:30 (manual): nobody is free until 10:00 (10 min buffer).
    fake.add("2026-10-05T09:00", "2026-10-05T09:30", summary="Staff meeting")
    # Our booking for Dr. Sara 10:00-10:30: Dr. Noura is still free.
    fake.add(
        "2026-10-05T10:00",
        "2026-10-05T10:30",
        extendedProperties={"private": {"wa_agent": "1", "staff_id": "dr-sara"}},
    )
    # A "free" reminder must not block anything.
    fake.add("2026-10-05T11:00", "2026-10-05T12:00", transparency="transparent")
    slots = gcal.get_availability(session, checkup, MONDAY, MONDAY, None, NOW)
    by_time = {_local(gcal, s.start): s.staff_id for s in slots}
    assert "09:00" not in by_time and "09:30" not in by_time
    assert by_time["10:00"] == "dr-noura"  # Sara is booked
    assert by_time["11:00"] == "dr-sara"
    sara_only = gcal.get_availability(session, checkup, MONDAY, MONDAY, "dr-sara", NOW)
    assert "10:00" not in {_local(gcal, s.start) for s in sara_only}
    assert sum(1 for m, _ in fake.calls if m == "GET") == 2  # one list call per lookup


def test_all_day_busy_event_closes_the_day(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    fake.add("2026-10-05", "2026-10-06", summary="Clinic closed")
    assert gcal.get_availability(session, _svc(kb, "checkup"), MONDAY, MONDAY, None, NOW) == []


# ------------------------------------------------------------------ create / move / cancel


def test_create_writes_a_tagged_event(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    _, customer = make_conversation(session, name="Omar")
    cleaning = _svc(kb, "cleaning")
    start = gcal.get_availability(session, cleaning, MONDAY, MONDAY, None, NOW)[0].start
    booking = gcal.create(session, customer, cleaning, start, None, "sensitive note", NOW)
    event = fake.events[str(booking.external_id)]
    assert event["summary"] == "Teeth cleaning – Omar"
    assert event["start"] == {"dateTime": "2026-10-05T06:00:00+00:00", "timeZone": "Asia/Riyadh"}
    assert event["extendedProperties"]["private"] == {
        "wa_agent": "1",
        "staff_id": "dr-sara",
        "service_id": "cleaning",
        "booking_id": str(booking.id),
    }
    assert "WhatsApp: +966500000001" in event["description"]
    assert "sensitive note" not in json.dumps(event)  # notes aren't written (PDPL)
    assert fake.token_requests == 1
    # The slot is now taken for Dr. Sara (the only cleaning staff).
    with pytest.raises(BookingError, match="slot_unavailable"):
        gcal.create(session, customer, cleaning, start, None, None, NOW)
    assert fake.token_requests == 1  # token reused


def test_reschedule_patches_and_ignores_its_own_event(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    _, customer = make_conversation(session)
    cleaning = _svc(kb, "cleaning")
    slots = gcal.get_availability(session, cleaning, MONDAY, MONDAY, None, NOW)
    booking = gcal.create(session, customer, cleaning, slots[0].start, None, None, NOW)
    gcal.check_reschedule(session, booking, slots[1].start, NOW)  # overlaps itself: fine
    gcal.reschedule(session, booking, slots[1].start, NOW)
    event = fake.events[str(booking.external_id)]
    assert event["start"]["dateTime"] == "2026-10-05T06:30:00+00:00"
    assert any(m == "PATCH" and u.endswith(f"/events/{booking.external_id}") for m, u in fake.calls)


def test_cancel_deletes_and_tolerates_already_deleted(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    _, customer = make_conversation(session)
    checkup = _svc(kb, "checkup")
    start = gcal.get_availability(session, checkup, MONDAY, MONDAY, None, NOW)[0].start
    a = gcal.create(session, customer, checkup, start, None, None, NOW)
    b = gcal.create(session, customer, checkup, start, None, None, NOW)  # the other dentist
    gcal.cancel(session, a, None, NOW)
    assert str(a.external_id) not in fake.events and a.status is BookingStatus.CANCELLED
    del fake.events[str(b.external_id)]  # staff deleted it by hand first
    gcal.cancel(session, b, None, NOW)
    assert b.status is BookingStatus.CANCELLED


def test_list_follows_changes_made_in_google_calendar(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    _, customer = make_conversation(session)
    checkup = _svc(kb, "checkup")
    slots = gcal.get_availability(session, checkup, MONDAY, MONDAY, None, NOW)
    moved = gcal.create(session, customer, checkup, slots[0].start, None, None, NOW)
    deleted = gcal.create(session, customer, checkup, slots[4].start, None, None, NOW)
    fake.events[str(moved.external_id)]["start"] = {"dateTime": "2026-10-06T12:00:00+03:00"}
    fake.events[str(moved.external_id)]["end"] = {"dateTime": "2026-10-06T12:30:00+03:00"}
    del fake.events[str(deleted.external_id)]
    [current] = gcal.list_for_customer(session, customer, NOW)
    assert current.id == moved.id
    assert current.start_at == datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    assert deleted.status is BookingStatus.CANCELLED


def test_calendar_down_raises_external_service_error(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    fake.down = True
    with pytest.raises(ExternalServiceError, match="google_calendar"):
        gcal.get_availability(session, _svc(kb, "checkup"), MONDAY, MONDAY, None, NOW)
    assert sum(1 for m, u in fake.calls if "calendar/v3" in u) == 3  # retried


def test_token_failure_is_an_external_service_error(
    session: Session, kb: KnowledgeBase, settings: Settings, sa_key_json: str
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    http = httpx.Client(transport=httpx.MockTransport(refuse))
    gcal = GoogleCalendarProvider(kb, settings.tz, CAL, load_credentials(sa_key_json), http=http)
    with pytest.raises(ExternalServiceError, match="token refresh failed"):
        gcal.get_availability(session, _svc(kb, "checkup"), MONDAY, MONDAY, None, NOW)


# ------------------------------------------------------------------ through the agent


def test_confirm_button_books_into_google_calendar(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    gcal: GoogleCalendarProvider,
    fake: FakeCalendar,
) -> None:
    from app.conversation.agent import AgentReplier
    from app.conversation.messages import CONFIRM_ID
    from app.conversation.replier import ReplyContext
    from tests.test_agent import ScriptedLLM, call

    conv, customer = make_conversation(session, name="Omar")
    llm = ScriptedLLM(
        call(
            "create_booking", service_id="whitening", start="2026-10-05 12:00", customer_name="Omar"
        )
    )
    agent = AgentReplier(settings, kb, llm, gcal, clock=lambda: NOW)
    ask = agent.reply(ReplyContext(session, conv, customer, "en", "whitening mon 12", None, None))
    assert ask is not None and ask.buttons and fake.events == {}
    done = agent.reply(ReplyContext(session, conv, customer, "en", "Yes", CONFIRM_ID, None))
    assert done is not None and done.text.startswith("You're booked ✅")
    [event] = fake.events.values()
    assert event["summary"] == "Teeth whitening – Omar"
    assert event["start"]["dateTime"] == (datetime(2026, 10, 5, 9, 0, tzinfo=UTC)).isoformat()


# ------------------------------------------------------------------ booking sync (§6)


def test_sync_follows_staff_edits_and_adopts_tagged_events(
    session: Session, gcal: GoogleCalendarProvider, kb: KnowledgeBase, fake: FakeCalendar
) -> None:
    _, customer = make_conversation(session)
    checkup = _svc(kb, "checkup")
    slots = gcal.get_availability(session, checkup, MONDAY, MONDAY, None, NOW)
    moved = gcal.create(session, customer, checkup, slots[0].start, None, None, NOW)
    deleted = gcal.create(session, customer, checkup, slots[4].start, None, None, NOW)
    same = gcal.create(session, customer, checkup, slots[8].start, None, None, NOW)
    fake.events[str(moved.external_id)]["start"] = {"dateTime": "2026-10-06T12:00:00+03:00"}
    fake.events[str(moved.external_id)]["end"] = {"dateTime": "2026-10-06T12:30:00+03:00"}
    del fake.events[str(deleted.external_id)]
    fake.add(
        "2026-10-07T11:00",
        "2026-10-07T11:45",
        summary="Teeth cleaning - phone booking",
        description="Booked by phone.\nWhatsApp: +966 55 123 4567",
    )
    fake.add("2026-10-07T13:00", "2026-10-07T14:00", summary="Staff meeting")  # untagged

    changed = gcal.sync_external(session, NOW)
    assert {b.id for b in changed if b.id in (moved.id, deleted.id)} == {moved.id, deleted.id}
    assert same not in changed
    assert moved.start_at == datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
    assert deleted.status is BookingStatus.CANCELLED
    [adopted] = [b for b in changed if b.source is BookingSource.EXTERNAL]
    assert adopted.service_id == "cleaning"
    owner = session.get(Customer, adopted.customer_id)
    assert owner is not None and owner.wa_id == "966551234567"
    assert gcal.sync_external(session, NOW) == []  # nothing new the second time
