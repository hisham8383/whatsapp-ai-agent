"""Booking provider interface and the scheduling rules every adapter shares (CLAUDE.md §6).

``BookingProvider`` is what the conversation tools call. Each adapter talks to
one kind of booking system (Google Calendar, Calendly, a client API, or our
own database). Every adapter also keeps a row in our ``bookings`` table, so
``list_for_customer`` and the staff inbox work the same whatever the backend.

The rules here come from ``business.yaml`` and are applied before any
provider call: opening hours, lead time, booking horizon, slot interval, and
the buffer between appointments. Times are UTC in the database and in these
functions; ``BUSINESS_TIMEZONE`` is used only to interpret opening hours and to
display times (§6).

``now`` is always passed in, so tests and the eval set can pin the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import BookingProviderName, Settings
from app.db.models import Booking, Customer
from app.kb.loader import KnowledgeBase, Service


class BookingError(Exception):
    """A booking request that breaks a rule. ``code`` is shown to the model, never raw to customers.

    Codes: ``slot_unavailable``, ``outside_hours``, ``too_soon``, ``too_far``,
    ``in_past``, ``not_found``, ``too_late_to_change``, ``not_active``.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Slot:
    """A bookable start time for one service."""

    start: datetime  # UTC
    end: datetime  # UTC
    staff_id: str | None


class BookingProvider(Protocol):
    """What every adapter implements. All datetimes are timezone-aware UTC."""

    def get_availability(
        self,
        session: Session,
        service: Service,
        day_from: date,
        day_to: date,
        staff_id: str | None,
        now: datetime,
    ) -> list[Slot]:
        """Free slots for ``service`` between two local dates (inclusive), earliest first.

        Raises:
            app.http.ExternalServiceError: The booking system is unreachable.
        """
        ...

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
        """Book ``service`` at ``start``. ``staff_id=None`` picks any free eligible staff.

        Raises:
            BookingError: The time breaks a rule or is no longer free.
            app.http.ExternalServiceError: The booking system is unreachable.
        """
        ...

    def reschedule(
        self, session: Session, booking: Booking, new_start: datetime, now: datetime
    ) -> Booking:
        """Move ``booking`` to ``new_start``, keeping the service.

        Raises:
            BookingError: The new time breaks a rule or isn't free.
            app.http.ExternalServiceError: The booking system is unreachable.
        """
        ...

    def check_reschedule(
        self, session: Session, booking: Booking, new_start: datetime, now: datetime
    ) -> None:
        """Raise ``BookingError`` if ``booking`` can't move to ``new_start``. Changes nothing.

        Used before asking the customer to confirm a move, so the bot never
        proposes a time it can't deliver.

        Raises:
            BookingError: The new time breaks a rule or isn't free.
            app.http.ExternalServiceError: The booking system is unreachable.
        """
        ...

    def cancel(self, session: Session, booking: Booking, reason: str | None, now: datetime) -> None:
        """Cancel ``booking``.

        Raises:
            app.http.ExternalServiceError: The booking system is unreachable.
        """
        ...

    def list_for_customer(
        self, session: Session, customer: Customer, now: datetime
    ) -> list[Booking]:
        """The customer's confirmed bookings that haven't started yet, earliest first."""
        ...

    def sync_external(self, session: Session, now: datetime) -> list[Booking]:
        """Pull changes made outside the agent (walk-ins, phone, staff edits), §6.

        Called by the scheduler every ``BOOKING_SYNC_MINUTES`` so reminders stay
        correct. Updates the ``bookings`` mirror in place.

        Returns:
            Bookings that were moved, cancelled, or newly found (``source=external``),
            so the caller can re-plan their reminders.

        Raises:
            app.http.ExternalServiceError: The booking system is unreachable.
        """
        ...


class UnsupportedBookingProviderError(ValueError):
    """``BOOKING_PROVIDER`` names an adapter that isn't built in this repo yet."""


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC (SQLite returns naive values in tests)."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def candidate_starts(
    kb: KnowledgeBase, service: Service, day: date, tz: ZoneInfo, now: datetime
) -> list[datetime]:
    """Start times on ``day`` (local) that fit opening hours, lead time, and horizon.

    Start times fall on the ``slot_interval_minutes`` grid from each opening
    range's start. The whole service must fit before the range closes.

    Returns:
        UTC datetimes, earliest first. Busy times are not considered here.
    """
    rules = kb.rules
    earliest = now + timedelta(hours=rules.min_lead_time_hours)
    latest = now + timedelta(days=rules.booking_horizon_days)
    duration = timedelta(minutes=service.duration_minutes)
    step = timedelta(minutes=rules.slot_interval_minutes)
    starts: list[datetime] = []
    for rng in kb.business.open_ranges(day.weekday()):
        t = datetime.combine(day, rng.start, tzinfo=tz)
        close = datetime.combine(day, rng.end, tzinfo=tz)
        while t + duration <= close:
            utc = t.astimezone(UTC)
            if earliest <= utc <= latest:
                starts.append(utc)
            t += step
    return starts


def check_start(
    kb: KnowledgeBase, service: Service, start: datetime, tz: ZoneInfo, now: datetime
) -> None:
    """Raise ``BookingError`` unless ``start`` is a time customers may book.

    Unlike ``candidate_starts`` it accepts off-grid times (e.g. 10:15), as long
    as the whole service fits inside an opening range.
    """
    if start <= now:
        raise BookingError("in_past")
    if start < now + timedelta(hours=kb.rules.min_lead_time_hours):
        raise BookingError("too_soon", f"book at least {kb.rules.min_lead_time_hours:g}h ahead")
    if start > now + timedelta(days=kb.rules.booking_horizon_days):
        raise BookingError("too_far", f"book at most {kb.rules.booking_horizon_days} days ahead")
    local = start.astimezone(tz)
    end = local + timedelta(minutes=service.duration_minutes)
    for rng in kb.business.open_ranges(local.date().weekday()):
        if datetime.combine(
            local.date(), rng.start, tzinfo=tz
        ) <= local and end <= datetime.combine(local.date(), rng.end, tzinfo=tz):
            return
    raise BookingError("outside_hours", "the business is closed then or the service would overrun")


def conflicts(
    start: datetime, end: datetime, busy: list[tuple[datetime, datetime]], buffer_minutes: int
) -> bool:
    """True if ``[start, end)`` is within ``buffer_minutes`` of any busy interval."""
    gap = timedelta(minutes=buffer_minutes)
    return any(start < b_end + gap and b_start < end + gap for b_start, b_end in busy)


def check_change_allowed(kb: KnowledgeBase, booking: Booking, now: datetime) -> None:
    """Raise ``BookingError`` if the policy forbids cancelling or moving ``booking`` now."""
    start = as_utc(booking.start_at)
    if start <= now:
        raise BookingError("in_past", "the appointment has already started")
    notice = timedelta(hours=kb.rules.min_cancel_notice_hours)
    if notice and start - now < notice:
        raise BookingError(
            "too_late_to_change",
            f"changes need {kb.rules.min_cancel_notice_hours:g}h notice",
        )


def build_booking_provider(settings: Settings, kb: KnowledgeBase) -> BookingProvider:
    """Return the adapter selected by ``BOOKING_PROVIDER``.

    Only the adapter the client needs gets built (§6). A provider without an
    adapter fails here, at startup, rather than on a customer's first booking.

    Raises:
        UnsupportedBookingProviderError: That adapter isn't implemented yet.
        app.booking.google_calendar.CalendarConfigError: Google Calendar is
            selected but ``CALENDAR_ID`` / ``CALENDAR_CREDENTIALS`` is unusable.
    """
    if settings.booking_provider is BookingProviderName.INTERNAL:
        from app.booking.internal import InternalProvider

        return InternalProvider(kb, settings.tz)
    if settings.booking_provider is BookingProviderName.GOOGLE_CALENDAR:
        from app.booking.google_calendar import GoogleCalendarProvider, load_credentials

        credentials = load_credentials(settings.calendar_credentials.get_secret_value())
        return GoogleCalendarProvider(kb, settings.tz, settings.calendar_id, credentials)
    raise UnsupportedBookingProviderError(
        f"BOOKING_PROVIDER={settings.booking_provider.value} has no adapter yet. "
        "Build it for this client (CLAUDE.md §6) or use 'internal'."
    )
