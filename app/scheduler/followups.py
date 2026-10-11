"""Lead follow-ups for customers who asked about a service but didn't book (CLAUDE.md §5).

1. The assistant calls ``mark_lead_interest(service_id)`` when a customer
   shows interest in a specific service (``record_interest``).
2. ``plan_due`` (every scheduler tick) looks at interests older than
   ``FOLLOWUP_DELAY_HOURS``. Each is decided exactly once (``handled_at``):
   a ``follow_ups`` row is queued unless the customer opted out, booked that service,
   is being handled by staff, or already got a follow-up within
   ``FOLLOWUP_COOLDOWN_DAYS``. The send time is moved out of ``QUIET_HOURS``.
3. ``FollowUpSender`` sends the ``TEMPLATE_FOLLOWUP`` template (MARKETING),
   re-checking the same rules at send time.

Why so many guards: this is the one message a customer didn't ask for. §4
requires opt-outs to be honored forever, §1 rules out broadcasts, and a
follow-up to someone who already booked makes the business look careless.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.conversation.outbound import store_template_message
from app.db.models import (
    Booking,
    BookingStatus,
    Conversation,
    ConversationState,
    Customer,
    FollowUp,
    LeadInterest,
    SendStatus,
)
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase
from app.logging import get_logger
from app.scheduler.quiet import shift_out_of_quiet
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

# Used for {{1}} when we don't know the customer's name (Meta rejects empty parameters).
NO_NAME = {"ar": "عميلنا العزيز", "en": "there"}


def first_name(name: str | None, language: str) -> str:
    """The first word of the customer's name, or a polite stand-in."""
    words = (name or "").split()
    return words[0][:40] if words else NO_NAME.get(language, NO_NAME["en"])


def record_interest(
    session: Session, customer: Customer, service_id: str, now: datetime
) -> LeadInterest | None:
    """Record that ``customer`` asked about ``service_id``. Returns ``None`` if already pending.

    Args:
        session: Open session; the worker commits.
        customer: The customer in the conversation.
        service_id: A service id from ``business.yaml`` (validated by the tool).
        now: Current time (pinned in evals).
    """
    pending = session.execute(
        select(LeadInterest.id).where(
            LeadInterest.customer_id == customer.id,
            LeadInterest.service_id == service_id,
            LeadInterest.handled_at.is_(None),
        )
    ).first()
    if pending is not None:
        return None
    interest = LeadInterest(customer_id=customer.id, service_id=service_id, created_at=now)
    session.add(interest)
    return interest


def _skip_reason(
    session: Session,
    settings: Settings,
    customer: Customer,
    service_id: str,
    since: datetime,
    now: datetime,
) -> str | None:
    """Why no follow-up about ``service_id`` may go to ``customer`` now, or ``None`` if it may.

    Only a booking of the *same* service counts as "booked": a customer with a
    cleaning tomorrow who asked about a kids check-up still gets that one
    follow-up (decided during live testing).
    """
    if customer.opted_out_followups:
        return "opted_out"
    booked = session.execute(
        select(Booking.id).where(
            Booking.customer_id == customer.id,
            Booking.service_id == service_id,
            (Booking.created_at >= since)
            | ((Booking.status == BookingStatus.CONFIRMED) & (Booking.start_at > now)),
        )
    ).first()
    if booked is not None:
        return "booked"
    human = session.execute(
        select(Conversation.id).where(
            Conversation.customer_id == customer.id,
            Conversation.state == ConversationState.HUMAN,
        )
    ).first()
    if human is not None:
        return "with_staff"
    recent = session.execute(
        select(FollowUp.id).where(
            FollowUp.customer_id == customer.id,
            FollowUp.status.in_([SendStatus.PENDING, SendStatus.SENT]),
            FollowUp.created_at > now - timedelta(days=settings.followup_cooldown_days),
        )
    ).first()
    if recent is not None:
        return "cooldown"
    return None


def plan_due(session: Session, settings: Settings, now: datetime) -> int:
    """Decide every lead interest older than ``FOLLOWUP_DELAY_HOURS``. Returns follow-ups queued.

    Args:
        session: Open session; the caller commits.
        settings: Delay, cooldown, quiet hours, template name.
        now: Current time.
    """
    cutoff = now - timedelta(hours=settings.followup_delay_hours)
    interests = (
        session.execute(
            select(LeadInterest)
            .where(LeadInterest.handled_at.is_(None), LeadInterest.created_at <= cutoff)
            .order_by(LeadInterest.created_at)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    queued = 0
    for interest in interests:
        interest.handled_at = now
        customer = session.get(Customer, interest.customer_id)
        if customer is None:
            continue
        reason = _skip_reason(
            session, settings, customer, interest.service_id, interest.created_at, now
        )
        if reason is not None:
            log.info("follow-up not queued", extra={"event": "followup_skip", "status": reason})
            continue
        language = customer.language if customer.language in ("ar", "en") else None
        session.add(
            FollowUp(
                customer_id=customer.id,
                service_id=interest.service_id,
                send_at=shift_out_of_quiet(now, settings.tz, settings.quiet_window),
                template_name=settings.template_followup,
                language=language or settings.default_language,
                created_at=now,
            )
        )
        session.flush()  # so the next interest of the same customer sees the cooldown
        queued += 1
    return queued


def cancel_pending_for(session: Session, customer_id: int) -> None:
    """Cancel queued follow-ups (the customer opted out or booked)."""
    for row in session.execute(
        select(FollowUp).where(
            FollowUp.customer_id == customer_id, FollowUp.status == SendStatus.PENDING
        )
    ).scalars():
        row.status = SendStatus.CANCELLED


class FollowUpSender:
    """Scheduler handler that sends one due follow-up."""

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
        """Send ``row`` (a locked ``FollowUp``) or mark it skipped."""
        assert isinstance(row, FollowUp)
        customer = session.get(Customer, row.customer_id)
        service = self._kb.service(row.service_id)
        if customer is None or service is None:
            mark_skipped(row)  # e.g. the service was removed from business.yaml
            return
        # Created within the cooldown by definition, so check everything but that.
        since = (
            row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=now.tzinfo)
        )
        reason = _skip_reason(session, self._settings, customer, row.service_id, since, now)
        if reason not in (None, "cooldown"):
            mark_skipped(row)
            return
        lang = row.language
        business = self._kb.business
        params = [
            first_name(customer.name, lang),
            service.name_ar if lang == "ar" else service.name_en,
            business.name_ar if lang == "ar" else business.name_en,
        ]
        try:
            wamid = self._client.send_template(customer.wa_id, row.template_name, lang, params)
        except ExternalServiceError as exc:
            handle_send_error(row, exc, now)
            return
        mark_sent(row, wamid, now)
        body = self._catalog.render(row.template_name, lang, params)
        after_send(
            session,
            "follow-up message",
            lambda: store_template_message(session, customer, body, wamid),
        )
        log.info("follow-up sent", extra={"event": "followup_sent", "wa_message_id": wamid})
