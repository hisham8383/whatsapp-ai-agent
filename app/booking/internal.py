"""``InternalProvider``: bookings live in our own Postgres (CLAUDE.md §6).

For clients with no booking system. Staff see upcoming bookings in the inbox.
Availability is computed from ``business.yaml`` (opening hours, service
durations, staff, buffers) minus the confirmed bookings already stored.

Double-booking protection: ``create`` and ``reschedule`` take a transaction-
scoped Postgres advisory lock per staff member, then re-check for conflicts
before writing. Two worker replicas booking the same slot at the same moment
therefore serialize, and the second one gets ``slot_unavailable``. (SQLite in
unit tests has a single writer, so no lock is needed there.)
"""

from __future__ import annotations

import zlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.booking.base import (
    BookingError,
    Slot,
    as_utc,
    candidate_starts,
    check_start,
    conflicts,
)
from app.db.models import Booking, BookingStatus, Customer
from app.kb.loader import KnowledgeBase, Service

# Keeps a single availability answer small enough for the model and the list message.
MAX_SLOTS = 40


class InternalProvider:
    """Implements ``app.booking.base.BookingProvider`` on the ``bookings`` table."""

    def __init__(self, kb: KnowledgeBase, tz: ZoneInfo) -> None:
        self._kb = kb
        self._tz = tz

    # ------------------------------------------------------------ queries

    def _busy(
        self,
        session: Session,
        staff_id: str | None,
        start: datetime,
        end: datetime,
        exclude_id: int | None = None,
    ) -> list[tuple[datetime, datetime]]:
        """Confirmed bookings for one staff member (or the business) overlapping a window."""
        gap = timedelta(minutes=self._kb.rules.buffer_minutes)
        stmt = select(Booking.start_at, Booking.end_at).where(
            Booking.status == BookingStatus.CONFIRMED,
            Booking.start_at < end + gap,
            Booking.end_at > start - gap,
            Booking.staff_id.is_(None) if staff_id is None else Booking.staff_id == staff_id,
        )
        if exclude_id is not None:
            stmt = stmt.where(Booking.id != exclude_id)
        return [(as_utc(s), as_utc(e)) for s, e in session.execute(stmt).all()]

    def _free_staff(
        self,
        session: Session,
        service: Service,
        start: datetime,
        candidates: list[str | None],
        exclude_id: int | None = None,
    ) -> str | bool | None:
        """First candidate free at ``start``, or ``False`` if nobody is."""
        end = start + timedelta(minutes=service.duration_minutes)
        for staff in candidates:
            busy = self._busy(session, staff, start, end, exclude_id)
            if not conflicts(start, end, busy, self._kb.rules.buffer_minutes):
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
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            key = zlib.crc32(f"booking:{staff or '-'}".encode())
            session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})

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
        """See ``BookingProvider.get_availability``."""
        candidates = self._candidates(service, staff_id)
        slots: list[Slot] = []
        day = day_from
        while day <= day_to and len(slots) < MAX_SLOTS:
            for start in candidate_starts(self._kb, service, day, self._tz, now):
                staff = self._free_staff(session, service, start, candidates)
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
        """See ``BookingProvider.create``. ``notes`` aren't stored (PDPL minimization, §2)."""
        check_start(self._kb, service, start, self._tz, now)
        candidates = self._candidates(service, staff_id)
        for staff in candidates:
            self._lock(session, staff)
        chosen = self._free_staff(session, service, start, candidates)
        if chosen is False:
            raise BookingError("slot_unavailable")
        booking = Booking(
            customer_id=customer.id,
            service_id=service.id,
            staff_id=chosen if isinstance(chosen, str) else None,
            start_at=start,
            end_at=start + timedelta(minutes=service.duration_minutes),
            status=BookingStatus.CONFIRMED,
        )
        session.add(booking)
        session.flush()
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
        chosen = self._free_staff(session, service, new_start, candidates, exclude_id=booking.id)
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
        """See ``BookingProvider.reschedule``. Keeps the staff member when they're free."""
        service, chosen = self._reschedule_target(session, booking, new_start, now, lock=True)
        booking.start_at = new_start
        booking.end_at = new_start + timedelta(minutes=service.duration_minutes)
        booking.staff_id = chosen
        session.flush()
        return booking

    def cancel(self, session: Session, booking: Booking, reason: str | None, now: datetime) -> None:
        """See ``BookingProvider.cancel``. The reason isn't stored (it may be personal)."""
        booking.status = BookingStatus.CANCELLED
        session.flush()

    def sync_external(self, session: Session, now: datetime) -> list[Booking]:
        """See ``BookingProvider.sync_external``. Nothing to pull: our Postgres is the system."""
        return []

    def list_for_customer(
        self, session: Session, customer: Customer, now: datetime
    ) -> list[Booking]:
        """See ``BookingProvider.list_for_customer``."""
        return list(
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
