"""Booking sync: keep reminders right when bookings change outside WhatsApp (CLAUDE.md §6).

Every ``BOOKING_SYNC_MINUTES`` the scheduler asks the booking adapter for
changes made elsewhere (``BookingProvider.sync_external``): staff moving or
deleting an appointment in Google Calendar, or adding a phone booking tagged
with the customer's WhatsApp number. For each changed booking the reminders
are re-planned, and when staff **moved** an appointment the customer booked
through WhatsApp, the customer is told with the ``TEMPLATE_BOOKING_UPDATE``
UTILITY template (they may be outside the 24h window, §4).

The internal adapter has nothing to pull: every change goes through the
agent, which re-plans reminders itself.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.booking.base import BookingProvider, as_utc
from app.config import Settings
from app.conversation.outbound import store_template_message
from app.conversation.timefmt import day_label, time_label
from app.db.models import Booking, BookingSource, BookingStatus, Customer
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase
from app.logging import get_logger
from app.scheduler import reminders
from app.scheduler.sending import after_send
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import TemplateCatalog

log = get_logger(__name__)


class BookingSync:
    """One sync pass per call; the scheduler decides how often."""

    def __init__(
        self,
        settings: Settings,
        kb: KnowledgeBase,
        provider: BookingProvider,
        client: WhatsAppClient,
        catalog: TemplateCatalog,
    ) -> None:
        self._settings = settings
        self._kb = kb
        self._provider = provider
        self._client = client
        self._catalog = catalog

    def run(self, session: Session, now: datetime) -> int:
        """Pull external changes and re-plan reminders. Returns bookings changed.

        Args:
            session: Open session; the caller commits.
            now: Current time.

        Failure modes:
            ``ExternalServiceError`` from the adapter propagates (the scheduler
            logs it and tries again next time). A failed update message is
            logged and not retried: the reminders still carry the right time.
        """
        changed = self._provider.sync_external(session, now)
        for booking in changed:
            replanned = reminders.ensure_current(session, self._settings, booking, now)
            if (
                replanned
                and booking.source is BookingSource.AGENT
                and booking.status is BookingStatus.CONFIRMED
            ):
                self._notify_moved(session, booking)
        return len(changed)

    def _notify_moved(self, session: Session, booking: Booking) -> None:
        customer = session.get(Customer, booking.customer_id)
        if customer is None:
            return
        lang = customer.language if customer.language in ("ar", "en") else None
        lang = lang or self._settings.default_language
        tz, start = self._settings.tz, as_utc(booking.start_at)
        business = self._kb.business
        params = [
            business.name_ar if lang == "ar" else business.name_en,
            day_label(start, tz, lang),
            time_label(start, tz),
        ]
        name = self._settings.template_booking_update
        try:
            wamid = self._client.send_template(customer.wa_id, name, lang, params)
        except ExternalServiceError as exc:
            log.error("booking update not sent: %s", exc, extra={"event": "update_failed"})
            return
        body = self._catalog.render(name, lang, params)
        after_send(
            session,
            "booking update",
            lambda: store_template_message(session, customer, body, wamid),
        )
        log.info("booking update sent", extra={"event": "update_sent", "wa_message_id": wamid})
