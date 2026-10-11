"""Appointment reminders (CLAUDE.md §5).

Scheduling: ``sync_for_booking`` runs whenever a booking is created, moved, or
cancelled (booking tools, booking sync). It cancels the booking's pending
reminders and, for a confirmed future booking, creates one per
``REMINDER_OFFSETS`` entry. Offsets whose time has already passed are
skipped: a "24 hours before" reminder for a booking made 3 hours ahead would
only confuse.

Sending: ``ReminderSender`` is the scheduler's handler for due rows. It checks
the rules again at send time (the booking may have been cancelled, the slot
may have passed, the customer may have opted out of reminders) and sends the
``TEMPLATE_REMINDER`` template in the customer's language, with "Confirm" /
"Reschedule" quick replies whose payloads route the tap back into the
conversation (``app/conversation/handler.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.booking.base import as_utc
from app.config import Settings
from app.conversation.outbound import store_template_message
from app.conversation.timefmt import day_label, time_label
from app.db.models import Booking, BookingStatus, Customer, Reminder, SendStatus
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase
from app.logging import get_logger
from app.scheduler.sending import (
    ScheduledSend,
    after_send,
    handle_send_error,
    mark_sent,
    mark_skipped,
)
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import TemplateCatalog

log = get_logger(__name__)

PAYLOAD_PREFIX = "rem"
CONFIRM = "confirm"
RESCHEDULE = "reschedule"


def payload(reminder_id: int, action: str) -> str:
    """Quick-reply payload, e.g. ``rem:42:reschedule``."""
    return f"{PAYLOAD_PREFIX}:{reminder_id}:{action}"


def parse_payload(reply_id: str | None) -> tuple[int, str] | None:
    """``rem:42:confirm`` -> ``(42, "confirm")``; anything else -> ``None``."""
    if not reply_id or not reply_id.startswith(f"{PAYLOAD_PREFIX}:"):
        return None
    parts = reply_id.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in (CONFIRM, RESCHEDULE):
        return None
    return int(parts[1]), parts[2]


def cancel_pending(session: Session, booking_id: int) -> int:
    """Cancel every pending reminder of a booking. Returns how many."""
    result = session.execute(
        update(Reminder)
        .where(Reminder.booking_id == booking_id, Reminder.status == SendStatus.PENDING)
        .values(status=SendStatus.CANCELLED)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def sync_for_booking(
    session: Session, settings: Settings, booking: Booking, now: datetime
) -> list[Reminder]:
    """Make the booking's pending reminders match its current time and status.

    Args:
        session: Open session; the caller commits (with the booking change, so
            both land together or not at all).
        settings: For ``REMINDER_OFFSETS``, the template name, and the default language.
        booking: Just created, moved, or cancelled. Must be flushed (has an id).
        now: Current time (pinned in evals).

    Returns:
        The reminders created (empty for a cancelled or past booking).
    """
    session.flush()
    cancel_pending(session, booking.id)
    if booking.status is not BookingStatus.CONFIRMED:
        return []
    # A reminder already sent for exactly this time (e.g. sent early by hand)
    # must not be planned again.
    already_sent = {
        as_utc(r.send_at)
        for r in session.execute(
            select(Reminder).where(
                Reminder.booking_id == booking.id, Reminder.status == SendStatus.SENT
            )
        ).scalars()
    }
    start = as_utc(booking.start_at)
    customer = session.get(Customer, booking.customer_id)
    language = (customer.language if customer else None) or settings.default_language
    created: list[Reminder] = []
    for offset in settings.reminder_deltas:
        send_at = start - offset
        if send_at <= now:
            continue  # too late for this one (§5: skipped if the time already passed)
        if send_at in already_sent:
            continue
        reminder = Reminder(
            booking_id=booking.id,
            send_at=send_at,
            template_name=settings.template_reminder,
            language=language if language in ("ar", "en") else settings.default_language,
        )
        session.add(reminder)
        created.append(reminder)
    session.flush()
    return created


def ensure_current(session: Session, settings: Settings, booking: Booking, now: datetime) -> bool:
    """Re-plan the booking's reminders only if they no longer match it. Returns True if re-planned.

    Idempotent, so it is safe to call after anything that *might* have
    changed a booking (a calendar sync, a customer listing their bookings).
    Sent reminders are left alone; only pending ones are compared.
    """
    start = as_utc(booking.start_at)
    expected: set[datetime] = set()
    if booking.status is BookingStatus.CONFIRMED and start > now:
        expected = {start - off for off in settings.reminder_deltas if start - off > now}
    # Pending reminders, plus ones already sent for a time still ahead (sent early).
    pending = {
        as_utc(r.send_at)
        for r in session.execute(
            select(Reminder).where(
                Reminder.booking_id == booking.id,
                Reminder.status.in_([SendStatus.PENDING, SendStatus.SENT]),
            )
        ).scalars()
        if r.status is SendStatus.PENDING or as_utc(r.send_at) > now
    }
    if pending == expected:
        return False
    sync_for_booking(session, settings, booking, now)
    return True


def offsets_inside_notice(settings: Settings, kb: KnowledgeBase) -> list[str]:
    """``REMINDER_OFFSETS`` entries shorter than the change-notice period.

    Their "Reschedule" button can never move the booking (the rules forbid it),
    so those taps go straight to staff. Logged as a warning at startup so the
    setup can be reconsidered with the client.
    """
    notice = timedelta(hours=kb.rules.min_cancel_notice_hours)
    return [
        f"{off.total_seconds() / 3600:g}h"
        for off in settings.reminder_deltas
        if notice and off < notice
    ]


def warn_offsets_inside_notice(settings: Settings, kb: KnowledgeBase) -> None:
    """Log a startup warning for ``offsets_inside_notice`` (called by every process)."""
    short = offsets_inside_notice(settings, kb)
    if short:
        log.warning(
            "reminders %s fall inside the %gh change-notice period: their Reschedule "
            "button hands the chat to staff instead of rebooking",
            ", ".join(short),
            kb.rules.min_cancel_notice_hours,
            extra={"event": "config_warning"},
        )


class ReminderSender:
    """Scheduler handler that sends one due reminder (see ``app/scheduler/sending.py``)."""

    def __init__(
        self,
        settings: Settings,
        kb: KnowledgeBase,
        client: WhatsAppClient,
        catalog: TemplateCatalog,
    ) -> None:
        self._settings = settings
        self._kb = kb
        self._client = client
        self._catalog = catalog

    def __call__(self, session: Session, row: ScheduledSend, now: datetime) -> None:
        """Send ``row`` (a locked ``Reminder``) or mark it skipped. See the module docstring."""
        assert isinstance(row, Reminder)
        booking = session.get(Booking, row.booking_id)
        customer = session.get(Customer, booking.customer_id) if booking else None
        if (
            booking is None
            or customer is None
            or booking.status is not BookingStatus.CONFIRMED
            or as_utc(booking.start_at) <= now
        ):
            mark_skipped(row)
            return
        if customer.opted_out_reminders:
            mark_skipped(row)  # §4: reminders stop only on a reminder opt-out
            return
        tz = self._settings.tz
        business = self._kb.business
        start = as_utc(booking.start_at)
        params = [
            business.name_ar if row.language == "ar" else business.name_en,
            day_label(start, tz, row.language),
            time_label(start, tz),
        ]
        try:
            wamid = self._client.send_template(
                customer.wa_id,
                row.template_name,
                row.language,
                params,
                quick_reply_payloads=[payload(row.id, CONFIRM), payload(row.id, RESCHEDULE)],
            )
        except ExternalServiceError as exc:
            handle_send_error(row, exc, now)
            return
        mark_sent(row, wamid, now)
        body = self._catalog.render(row.template_name, row.language, params)
        after_send(
            session,
            "reminder message",
            lambda: store_template_message(session, customer, body, wamid),
        )
        log.info("reminder sent", extra={"event": "reminder_sent", "wa_message_id": wamid})


def upcoming_for(session: Session, booking_ids: list[int]) -> dict[int, list[Reminder]]:
    """Reminders per booking, for the inbox's booking list (§5)."""
    if not booking_ids:
        return {}
    rows = session.execute(
        select(Reminder)
        .where(Reminder.booking_id.in_(booking_ids), Reminder.status != SendStatus.CANCELLED)
        .order_by(Reminder.send_at)
    ).scalars()
    out: dict[int, list[Reminder]] = {}
    for r in rows:
        out.setdefault(r.booking_id, []).append(r)
    return out
