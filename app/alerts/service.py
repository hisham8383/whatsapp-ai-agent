"""Staff alerts when a conversation is handed to a human (CLAUDE.md §5).

``queue_handoff_alerts`` runs in the worker, in the same transaction as the
handoff: one ``staff_alerts`` row per ``HANDOFF_CONTACTS`` number (WhatsApp)
and per ``ALERT_EMAILS`` address (email), plus a ``send_staff_alerts`` job.

The worker picks that job up about a second later and sends the alerts
(``AlertJob``). The scheduler still sends any alert left pending (worker down,
a retry after a failed send), so nothing is lost. Both claim rows with
``FOR UPDATE SKIP LOCKED``, so an alert is never sent twice. Sending from the
worker removes the scheduler's up-to-a-minute delay, and with it the case,
seen in live testing, where staff clicked "Return to bot" before the next
scheduler tick and the alert was (correctly) skipped.

The WhatsApp alert is the ``TEMPLATE_STAFF_ALERT`` UTILITY template (customer
first name + handoff reason) in ``DEFAULT_LANGUAGE``: staff numbers usually
haven't messaged the business in the last 24 hours, so free text would be
refused (§4). Staff numbers must be able to receive it; test during UAT.

The email carries the bot's handoff summary and an inbox link. Neither
channel includes the customer's phone number (§2 data minimization): staff
open the inbox to see the conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.email import EmailSender
from app.config import Settings
from app.db.models import (
    AlertChannel,
    Conversation,
    ConversationState,
    Customer,
    SendStatus,
    StaffAlert,
)
from app.http import ExternalServiceError
from app.logging import get_logger, redact_phone
from app.scheduler.followups import first_name
from app.scheduler.sending import (
    ScheduledSend,
    handle_send_error,
    mark_sent,
    mark_skipped,
    process_row,
)
from app.whatsapp.client import WhatsAppClient
from app.worker import queue

log = get_logger(__name__)

# handoff_to_human reasons (app/conversation/tools.py) in staff-facing words.
REASONS: dict[str, dict[str, str]] = {
    "asked_for_human": {"ar": "طلب التحدث مع موظف", "en": "asked for a person"},
    "complaint": {"ar": "شكوى", "en": "complaint"},
    "out_of_scope": {"ar": "سؤال خارج نطاق المساعد", "en": "question the assistant can't answer"},
    "discount_or_exception": {"ar": "طلب خصم أو استثناء", "en": "discount or exception request"},
    "repeated_failure": {"ar": "المساعد تعثر مرتين", "en": "the assistant failed twice"},
    "late_change": {
        "ar": "يبي يغير موعد قريب (داخل مهلة التعديل)",
        "en": "wants to change a booking inside the notice period",
    },
    "other": {"ar": "يحتاج مساعدة من الفريق", "en": "needs the team's help"},
}


def reason_label(reason: str | None, language: str) -> str:
    """Staff-facing text for a handoff reason code."""
    labels = REASONS.get(reason or "other", REASONS["other"])
    return labels["ar" if language == "ar" else "en"]


def queue_handoff_alerts(
    session: Session, settings: Settings, conversation: Conversation, now: datetime
) -> int:
    """Create one alert row per staff contact. Returns how many.

    Args:
        session: Open session; committed with the handoff itself.
        settings: ``HANDOFF_CONTACTS`` and ``ALERT_EMAILS``.
        conversation: Just moved to ``HUMAN``.
        now: Current time; alerts are due immediately (no quiet hours: a
            customer is waiting).
    """
    rows = [
        StaffAlert(
            conversation_id=conversation.id,
            contact=number,
            channel=AlertChannel.WHATSAPP,
            send_at=now,
            created_at=now,
        )
        for number in settings.staff_numbers
    ] + [
        StaffAlert(
            conversation_id=conversation.id,
            contact=email,
            channel=AlertChannel.EMAIL,
            send_at=now,
            created_at=now,
        )
        for email in settings.alert_email_list
    ]
    session.add_all(rows)
    if rows:
        # Committed together with the rows: the worker sends them right away.
        queue.enqueue(session, queue.JOB_SEND_ALERTS, {"conversation_id": conversation.id})
    log.info(
        "handoff alerts queued",
        extra={"conversation_id": conversation.id, "event": "alerts_queued", "count": len(rows)},
    )
    return len(rows)


class AlertJob:
    """Worker handler for ``send_staff_alerts`` jobs: send a handoff's alerts now."""

    def __init__(self, sender: AlertSender, clock: Callable[[], datetime] | None = None) -> None:
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, session: Session, payload: dict[str, Any]) -> None:
        """Send the conversation's due alerts, in the job's transaction.

        Rows the scheduler is already sending are skipped (``SKIP LOCKED``);
        a failed send keeps its one retry, which the scheduler picks up.
        """
        now = self._clock()
        rows = (
            session.execute(
                select(StaffAlert)
                .where(
                    StaffAlert.conversation_id == int(payload["conversation_id"]),
                    StaffAlert.status == SendStatus.PENDING,
                    StaffAlert.send_at <= now,
                )
                .order_by(StaffAlert.id)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for row in rows:
            process_row(session, row, self._sender, now)


class AlertSender:
    """Scheduler handler that sends one due staff alert."""

    def __init__(
        self, settings: Settings, client: WhatsAppClient, email: EmailSender | None = None
    ) -> None:
        self._settings = settings
        self._client = client
        self._email = email

    def __call__(self, session: Session, row: ScheduledSend, now: datetime) -> None:
        """Send ``row`` (a locked ``StaffAlert``), or skip it if staff already took over."""
        assert isinstance(row, StaffAlert)
        conv = session.get(Conversation, row.conversation_id)
        customer = session.get(Customer, conv.customer_id) if conv else None
        if conv is None or customer is None or conv.state is not ConversationState.HUMAN:
            mark_skipped(row)  # already returned to the bot: nobody needs waking up
            return
        lang = self._settings.default_language
        try:
            if row.channel is AlertChannel.WHATSAPP:
                wamid: str | None = self._client.send_template(
                    row.contact,
                    self._settings.template_staff_alert,
                    lang,
                    [first_name(customer.name, lang), reason_label(conv.handoff_reason, lang)],
                )
            else:
                if self._email is None:
                    mark_skipped(row)  # ALERT_EMAILS removed after the row was queued
                    return
                self._email.send(row.contact, *self._email_text(conv, customer))
                wamid = None
        except ExternalServiceError as exc:
            handle_send_error(row, exc, now)
            return
        mark_sent(row, wamid, now)
        log.info(
            "staff alert sent",
            extra={
                "conversation_id": conv.id,
                "event": "alert_sent",
                "wa_id": redact_phone(row.contact),
                "status": row.channel.value,
            },
        )

    def _email_text(self, conv: Conversation, customer: Customer) -> tuple[str, str]:
        name = first_name(customer.name, "en")
        link = (
            f"{self._settings.public_base_url.rstrip('/')}/inbox/c/{conv.id}"
            if self._settings.public_base_url
            else "the staff inbox (/inbox)"
        )
        subject = f"WhatsApp: {name} needs a reply ({reason_label(conv.handoff_reason, 'en')})"
        body = (
            f"A customer on WhatsApp was handed over to the team.\n\n"
            f"Customer: {name}\n"
            f"Reason: {reason_label(conv.handoff_reason, 'en')}\n"
            f"Summary: {conv.handoff_summary or '-'}\n\n"
            f"Reply from {link}.\n"
            f"The assistant stays silent in this chat until you click 'Return to bot'."
        )
        return subject, body
