"""``GoogleCalendarProvider``: bookings are events in the client's Google Calendar (CLAUDE.md §6).

Access: a service account in the client's Google Cloud project. The client
shares the booking calendar with the service account's email ("Make changes to
events"). The key JSON is the ``CALENDAR_CREDENTIALS`` secret; the calendar is
``CALENDAR_ID``. Nobody's Google password is ever involved.

How the calendar maps to bookings:

* **One calendar** holds every appointment. Events the assistant creates carry
  private extended properties (``wa_agent``, ``staff_id``, ``service_id``,
  ``booking_id``), so each staff member can be booked in parallel.
* **Events added by hand** (no ``staff_id`` tag) block *every* staff member for
  their duration: a "clinic closed" or "meeting" entry must never be booked
  over. Events marked "free" (transparent) and cancelled events are ignored.
* Our ``bookings`` table mirrors the events (``external_id`` = event id), so
  the staff inbox and ``list_for_customer`` work as with every adapter. When a
  customer lists their bookings, each event is re-read: if staff deleted or
  moved it in Google Calendar, the mirror follows.
* **Booking sync** (``sync_external``, every ``BOOKING_SYNC_MINUTES``) does the
  same for every upcoming booking, so reminders follow staff edits. It also
  adopts events staff add by hand **if** their description has a line like
  ``WhatsApp: +966 5x xxx xxxx``: those become ``source=external`` bookings and
  get reminders too. Untagged manual events only block time.

Opening hours, lead time, horizon, buffer and slot interval still come from
``business.yaml`` (``app/booking/base.py``); the calendar only supplies busy times.

All HTTP goes through ``request_with_retry`` (timeouts, backoff). Token
refresh uses ``google-auth`` over the same ``httpx`` client.
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import google.auth.exceptions
import httpx
from google.auth import transport
from google.oauth2 import service_account
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.booking.base import BookingError, Slot, as_utc, candidate_starts, check_start, conflicts
from app.db.models import Booking, BookingSource, BookingStatus, Customer
from app.http import DEFAULT_TIMEOUT, ExternalServiceError, request_with_retry
from app.kb.loader import KnowledgeBase, Service
from app.logging import get_logger

log = get_logger(__name__)

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MAX_SLOTS = 40
AGENT_TAG = "wa_agent"

# "WhatsApp: +966 50 123 4567" (or واتساب / جوال) in a hand-made event's description.
_WHATSAPP_LINE = re.compile(r"(?:whatsapp|واتساب|جوال)\s*[:：]?\s*(\+?\d[\d\s-]{6,17}\d)", re.I)
EXTERNAL_SERVICE = "external"  # service_id for adopted events that name no known service

Busy = dict[str | None, list[tuple[datetime, datetime]]]  # staff id (None = everyone) -> intervals


class CalendarConfigError(ValueError):
    """``CALENDAR_ID`` or ``CALENDAR_CREDENTIALS`` is missing or unusable (fails at startup)."""


# ------------------------------------------------------------------ google-auth over httpx


class _AuthResponse(transport.Response):
    def __init__(self, response: httpx.Response) -> None:
        self._r = response

    @property
    def status(self) -> int:
        return self._r.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._r.headers

    @property
    def data(self) -> bytes:
        return self._r.content


class _AuthRequest(transport.Request):
    """Lets google-auth refresh tokens through our ``httpx`` client (no ``requests`` needed)."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> transport.Response:
        try:
            r = self._http.request(
                method, url, content=body, headers=headers, timeout=timeout or 10
            )
        except httpx.HTTPError as exc:
            raise google.auth.exceptions.TransportError(exc) from exc
        return _AuthResponse(r)


def load_credentials(raw: str) -> service_account.Credentials:
    """Parse the service-account key from ``CALENDAR_CREDENTIALS``.

    In production the secret store holds the key JSON itself. For local
    development the value may instead be the path to the downloaded key file
    (kept outside the repo).

    Raises:
        CalendarConfigError: Not JSON (or a readable JSON file), or not a
            service-account key.
    """
    value = raw.strip()
    if value and not value.startswith("{") and Path(value).is_file():
        value = Path(value).read_text(encoding="utf-8")
    try:
        info = json.loads(value)
        creds: service_account.Credentials = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
        return creds
    except (ValueError, KeyError, TypeError) as exc:
        raise CalendarConfigError(
            "CALENDAR_CREDENTIALS must be the service account's JSON key"
        ) from exc


# ------------------------------------------------------------------ provider


class GoogleCalendarProvider:
    """Implements ``app.booking.base.BookingProvider`` on a Google Calendar."""

    def __init__(
        self,
        kb: KnowledgeBase,
        tz: ZoneInfo,
        calendar_id: str,
        credentials: service_account.Credentials,
        http: httpx.Client | None = None,
    ) -> None:
        """
        Args:
            kb: Knowledge base (services, staff, rules).
            tz: ``BUSINESS_TIMEZONE``.
            calendar_id: ``CALENDAR_ID``, e.g. ``abc@group.calendar.google.com``.
            credentials: From ``load_credentials``.
            http: Injected in tests (``httpx.MockTransport``).

        Raises:
            CalendarConfigError: ``calendar_id`` is empty.
        """
        if not calendar_id.strip():
            raise CalendarConfigError(
                "CALENDAR_ID is required for BOOKING_PROVIDER=google_calendar"
            )
        self._kb = kb
        self._tz = tz
        self._events_url = f"{API}/calendars/{quote(calendar_id.strip(), safe='')}/events"
        self._creds = credentials
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT)

    # ------------------------------------------------------------ HTTP

    def _token(self) -> str:
        if not self._creds.valid:
            try:
                self._creds.refresh(_AuthRequest(self._http))
            except google.auth.exceptions.GoogleAuthError as exc:
                raise ExternalServiceError(
                    "google_calendar", f"token refresh failed: {exc}"
                ) from exc
        return str(self._creds.token)

    def _call(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        ok_statuses: tuple[int, ...] = (),
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token()}"}

        def send() -> httpx.Response:
            r = self._http.request(method, url, params=params, json=body, headers=headers)
            # Treat "already gone" on delete/get as success rather than an error.
            if r.status_code in ok_statuses:
                return httpx.Response(200, json={"status": "cancelled"}, request=r.request)
            return r

        return request_with_retry(send, service="google_calendar")

    def _list_events(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        params = {
            "timeMin": start.astimezone(UTC).isoformat(),
            "timeMax": end.astimezone(UTC).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "2500",
        }
        while True:
            data = self._call("GET", self._events_url, params=params).json()
            events += data.get("items", [])
            if not data.get("nextPageToken"):
                return events
            params["pageToken"] = data["nextPageToken"]

    # ------------------------------------------------------------ busy times

    def _event_time(self, value: dict[str, Any]) -> datetime:
        if "dateTime" in value:
            return datetime.fromisoformat(value["dateTime"]).astimezone(UTC)
        # All-day event: dates are local; the end date is exclusive.
        day = date.fromisoformat(value["date"])
        return datetime.combine(day, time.min, tzinfo=self._tz).astimezone(UTC)

    def _busy(self, start: datetime, end: datetime, exclude_event: str | None = None) -> Busy:
        busy: Busy = {}
        gap = timedelta(minutes=self._kb.rules.buffer_minutes)
        for ev in self._list_events(start - gap, end + gap):
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue
            if exclude_event and ev.get("id") == exclude_event:
                continue
            private = (ev.get("extendedProperties") or {}).get("private") or {}
            # Our own events block their staff member; anything else blocks everyone.
            staff = (private.get("staff_id") or None) if private.get(AGENT_TAG) else None
            interval = (self._event_time(ev["start"]), self._event_time(ev["end"]))
            busy.setdefault(staff, []).append(interval)
        return busy

    def _free_staff(
        self, busy: Busy, service: Service, start: datetime, candidates: list[str | None]
    ) -> str | bool | None:
        end = start + timedelta(minutes=service.duration_minutes)
        for staff in candidates:
            if staff is None:  # no staff list: the business is one resource, every event counts
                blocking = [i for intervals in busy.values() for i in intervals]
            else:
                blocking = busy.get(None, []) + busy.get(staff, [])
            if not conflicts(start, end, blocking, self._kb.rules.buffer_minutes):
                return staff
        return False

    def _candidates(self, service: Service, staff_id: str | None) -> list[str | None]:
        eligible = self._kb.staff_for(service)
        if staff_id is None:
            return eligible
        if staff_id not in eligible:
            raise BookingError("slot_unavailable", f"staff {staff_id} doesn't offer {service.id}")
        return [staff_id]

    @staticmethod
    def _lock(session: Session, staff: str | None) -> None:
        # Serializes our own workers; a person editing the calendar at the same
        # second is the remaining (tiny) race, and Google has no transactions.
        if session.get_bind().dialect.name == "postgresql":
            key = zlib.crc32(f"booking:{staff or '-'}".encode())
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

    # ------------------------------------------------------------ events

    def _event_body(
        self, booking: Booking, service: Service, customer: Customer | None
    ) -> dict[str, Any]:
        staff = self._kb.staff_member(booking.staff_id) if booking.staff_id else None
        who = (customer.name if customer else None) or "WhatsApp customer"
        lines = [
            "Booked by the WhatsApp assistant.",
            f"Service: {service.name_en} / {service.name_ar}",
            *([f"Staff: {staff.name_en} / {staff.name_ar}"] if staff else []),
            f"Customer: {who}",
            *([f"WhatsApp: +{customer.wa_id}"] if customer else []),
        ]
        tzname = str(self._tz)
        return {
            "summary": f"{service.name_en} – {who}",
            "description": "\n".join(lines),
            "start": {"dateTime": as_utc(booking.start_at).isoformat(), "timeZone": tzname},
            "end": {"dateTime": as_utc(booking.end_at).isoformat(), "timeZone": tzname},
            "extendedProperties": {
                "private": {
                    AGENT_TAG: "1",
                    "staff_id": booking.staff_id or "",
                    "service_id": service.id,
                    "booking_id": str(booking.id),
                }
            },
        }

    # ------------------------------------------------------------ interface

    def get_availability(
        self,
        session: Session,
        service: Service,
        day_from: date,
        day_to: date,
        staff_id: str | None,
        now: datetime,
    ) -> list[Slot]:
        """See ``BookingProvider.get_availability``. One events.list call for the whole range."""
        candidates = self._candidates(service, staff_id)
        window_start = datetime.combine(day_from, time.min, tzinfo=self._tz)
        window_end = datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=self._tz)
        busy = self._busy(window_start, window_end)
        slots: list[Slot] = []
        day = day_from
        while day <= day_to and len(slots) < MAX_SLOTS:
            for start in candidate_starts(self._kb, service, day, self._tz, now):
                staff = self._free_staff(busy, service, start, candidates)
                if staff is not False:
                    end = start + timedelta(minutes=service.duration_minutes)
                    slots.append(Slot(start, end, staff if isinstance(staff, str) else None))
                    if len(slots) >= MAX_SLOTS:
                        break
            day += timedelta(days=1)
        return slots

    def create(
        self,
        session: Session,
        customer: Customer,
        service: Service,
        start: datetime,
        staff_id: str | None,
        notes: str | None,
        now: datetime,
    ) -> Booking:
        """See ``BookingProvider.create``. ``notes`` aren't written (PDPL minimization, §2)."""
        check_start(self._kb, service, start, self._tz, now)
        candidates = self._candidates(service, staff_id)
        for staff in candidates:
            self._lock(session, staff)
        end = start + timedelta(minutes=service.duration_minutes)
        chosen = self._free_staff(self._busy(start, end), service, start, candidates)
        if chosen is False:
            raise BookingError("slot_unavailable")
        booking = Booking(
            customer_id=customer.id,
            service_id=service.id,
            staff_id=chosen if isinstance(chosen, str) else None,
            start_at=start,
            end_at=end,
            status=BookingStatus.CONFIRMED,
        )
        session.add(booking)
        session.flush()  # the event carries our booking id
        event = self._call(
            "POST", self._events_url, body=self._event_body(booking, service, customer)
        )
        booking.external_id = str(event.json()["id"])
        session.flush()
        log.info("calendar event created", extra={"event": "gcal_create", "booking_id": booking.id})
        return booking

    def _reschedule_target(
        self, session: Session, booking: Booking, new_start: datetime, now: datetime, lock: bool
    ) -> tuple[Service, str | None]:
        service = self._kb.service(booking.service_id)
        if service is None:
            raise BookingError("not_found", f"service {booking.service_id} no longer offered")
        check_start(self._kb, service, new_start, self._tz, now)
        eligible = self._kb.staff_for(service)
        candidates = [booking.staff_id] + [s for s in eligible if s != booking.staff_id]
        if lock:
            for staff in candidates:
                self._lock(session, staff)
        end = new_start + timedelta(minutes=service.duration_minutes)
        busy = self._busy(new_start, end, exclude_event=booking.external_id)
        chosen = self._free_staff(busy, service, new_start, candidates)
        if chosen is False:
            raise BookingError("slot_unavailable")
        return service, chosen if isinstance(chosen, str) else None

    def check_reschedule(
        self, session: Session, booking: Booking, new_start: datetime, now: datetime
    ) -> None:
        """See ``BookingProvider.check_reschedule``."""
        self._reschedule_target(session, booking, new_start, now, lock=False)

    def reschedule(
        self, session: Session, booking: Booking, new_start: datetime, now: datetime
    ) -> Booking:
        """See ``BookingProvider.reschedule``. Patches the event's time (and staff tag)."""
        service, chosen = self._reschedule_target(session, booking, new_start, now, lock=True)
        booking.start_at = new_start
        booking.end_at = new_start + timedelta(minutes=service.duration_minutes)
        booking.staff_id = chosen
        if booking.external_id:
            customer = session.get(Customer, booking.customer_id)
            body = self._event_body(booking, service, customer)
            self._call(
                "PATCH", f"{self._events_url}/{quote(booking.external_id, safe='')}", body=body
            )
        session.flush()
        return booking

    def cancel(self, session: Session, booking: Booking, reason: str | None, now: datetime) -> None:
        """See ``BookingProvider.cancel``. Deletes the event; an already-deleted one is fine."""
        if booking.external_id:
            url = f"{self._events_url}/{quote(booking.external_id, safe='')}"
            self._call("DELETE", url, ok_statuses=(404, 410))
        booking.status = BookingStatus.CANCELLED
        session.flush()

    def list_for_customer(
        self, session: Session, customer: Customer, now: datetime
    ) -> list[Booking]:
        """See ``BookingProvider.list_for_customer``. Syncs each booking with its event first."""
        bookings = list(
            session.execute(
                select(Booking)
                .where(
                    Booking.customer_id == customer.id,
                    Booking.status == BookingStatus.CONFIRMED,
                    Booking.start_at > now,
                )
                .order_by(Booking.start_at)
            ).scalars()
        )
        current = []
        for b in bookings:
            if b.external_id:
                url = f"{self._events_url}/{quote(b.external_id, safe='')}"
                ev = self._call("GET", url, ok_statuses=(404, 410)).json()
                if ev.get("status") == "cancelled":
                    b.status = BookingStatus.CANCELLED  # deleted by staff in Google Calendar
                    continue
                start = self._event_time(ev["start"])
                if start != as_utc(b.start_at):  # moved by staff in Google Calendar
                    b.start_at, b.end_at = start, self._event_time(ev["end"])
            if as_utc(b.start_at) > now:
                current.append(b)
        session.flush()
        return current

    def sync_external(self, session: Session, now: datetime) -> list[Booking]:
        """See ``BookingProvider.sync_external``. One events.list call over the booking horizon.

        * Our upcoming bookings follow their events: deleted -> cancelled,
          moved -> new start/end.
        * Hand-made events with a ``WhatsApp: +...`` line are adopted as
          ``source=external`` bookings (customer found or created by number).
        """
        horizon = now + timedelta(days=self._kb.rules.booking_horizon_days + 1)
        events = {ev["id"]: ev for ev in self._list_events(now, horizon) if ev.get("id")}
        changed: list[Booking] = []
        ours = session.execute(
            select(Booking).where(
                Booking.status == BookingStatus.CONFIRMED,
                Booking.start_at > now,
                Booking.external_id.is_not(None),
            )
        ).scalars()
        for b in ours:
            assert b.external_id is not None
            ev = events.get(b.external_id)
            if ev is None:  # deleted, or moved past the horizon: ask for it directly
                url = f"{self._events_url}/{quote(b.external_id, safe='')}"
                ev = self._call("GET", url, ok_statuses=(404, 410)).json()
            if ev.get("status") == "cancelled":
                b.status = BookingStatus.CANCELLED
                changed.append(b)
                continue
            start, end = self._event_time(ev["start"]), self._event_time(ev["end"])
            if start != as_utc(b.start_at) or end != as_utc(b.end_at):
                b.start_at, b.end_at = start, end
                changed.append(b)

        known = set(
            session.execute(
                select(Booking.external_id).where(Booking.external_id.in_(list(events)))
            ).scalars()
        )
        for event_id, ev in events.items():
            if event_id in known or ev.get("status") == "cancelled":
                continue
            private = (ev.get("extendedProperties") or {}).get("private") or {}
            if private.get(AGENT_TAG) or "dateTime" not in ev.get("start", {}):
                continue  # ours (already mirrored) or an all-day block
            match = _WHATSAPP_LINE.search(ev.get("description") or "")
            if match is None:
                continue  # a plain busy block: nobody to remind
            digits = re.sub(r"\D", "", match.group(1))
            if not 8 <= len(digits) <= 15:
                continue
            customer = self._customer(session, digits)
            booking = Booking(
                customer_id=customer.id,
                external_id=event_id,
                service_id=self._service_from_title(ev.get("summary") or ""),
                staff_id=None,
                start_at=self._event_time(ev["start"]),
                end_at=self._event_time(ev["end"]),
                status=BookingStatus.CONFIRMED,
                source=BookingSource.EXTERNAL,
            )
            session.add(booking)
            changed.append(booking)
        session.flush()
        if changed:
            log.info("calendar sync", extra={"event": "gcal_sync", "count": len(changed)})
        return changed

    @staticmethod
    def _customer(session: Session, wa_id: str) -> Customer:
        customer = session.execute(
            select(Customer).where(Customer.wa_id == wa_id)
        ).scalar_one_or_none()
        if customer is None:
            customer = Customer(wa_id=wa_id)
            session.add(customer)
            session.flush()
        return customer

    def _service_from_title(self, title: str) -> str:
        """The service whose English or Arabic name appears in the event title."""
        lowered = title.lower()
        for svc in self._kb.services:
            if svc.name_en.lower() in lowered or svc.name_ar in title:
                return svc.id
        return EXTERNAL_SERVICE
