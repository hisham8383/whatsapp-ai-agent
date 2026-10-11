"""Inbound handler: turns a stored customer message into a reply.

Runs inside the worker for each ``handle_inbound`` job. For each job:

1. Load the conversation, customer, and inbound message. A message older than
   one the customer already sent (Meta redelivering after an outage) is kept
   for the record but not answered: a reply to it would be out of context.
   Opt-outs in it still count.
2. Detect the reply language (§5: reply in the customer's language).
3. Opt-outs ("STOP", "إيقاف", "stop reminders", ...) are recorded and
   confirmed once, in any state, without the LLM (§4).
4. Reminder quick replies: "Confirm" gets a fixed thank-you; "Reschedule"
   becomes a reschedule request for the assistant, in ``BOT`` state (§5).
   Inside the business's change-notice period (``min_cancel_notice_hours``)
   the booking can't be moved by the assistant, so the customer gets the
   policy in a fixed message and the chat goes to staff, with the reason.
5. If the conversation is in ``HUMAN`` state, stay silent (§5), unless
   ``HANDOFF_AUTO_RELEASE_HOURS`` has passed, in which case return to ``BOT``.
6. Voice notes, images, and stickers get a polite request to type instead (§4).
7. Otherwise ask the ``Replier`` for a reply and send it: plain text, or an
   interactive list when the reply offers time slots (§4).
8. If this turn handed the conversation to staff, queue the staff alerts
   (WhatsApp template + optional email) in the same transaction (§5).

Every send goes through ``WhatsAppClient``, which refuses to send outside the
24h window. A refusal is logged, not retried: retrying can't reopen the window.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts.service import queue_handoff_alerts
from app.booking.base import BookingError, as_utc, check_change_allowed
from app.config import Settings
from app.conversation.language import detect_language
from app.conversation.replier import Replier, Reply, ReplyContext
from app.conversation.timefmt import day_label, time_label
from app.conversation.tools import hand_off
from app.db.models import (
    Booking,
    BookingStatus,
    Conversation,
    ConversationState,
    Customer,
    Direction,
    FollowUp,
    Message,
    MessageStatus,
    Reminder,
    Sender,
)
from app.kb.loader import KnowledgeBase
from app.logging import get_logger
from app.scheduler import followups, reminders
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.optout import Classification, classify
from app.whatsapp.window import OutsideServiceWindowError

log = get_logger(__name__)

# Canned bilingual replies that don't need the LLM.
CANNED: dict[str, dict[str, str]] = {
    "unsupported_media": {
        "ar": "معليش، ما أقدر أفتح الرسايل الصوتية أو الصور أو الملصقات. تقدر تكتب طلبك؟",
        "en": "Sorry, I can't open voice notes, images, or stickers. Could you type your message?",
    },
    "opt_out_followups": {
        "ar": "تم، ما راح نرسل لك رسائل متابعة بعد الحين. وتقدر تراسلنا هنا متى ما احتجت.",
        "en": "Done, we won't send you follow-up messages any more. "
        "You can still message us here any time.",
    },
    "opt_out_reminders": {
        "ar": "تم، أوقفنا تذكيرات المواعيد. ومواعيدك باقية زي ما هي.",
        "en": "Done, we've turned off appointment reminders. Your bookings stay as they are.",
    },
    "reminder_confirmed": {
        "ar": "أبشر، موعدك مؤكد وننتظرك!",
        "en": "Great, your appointment is confirmed. See you then!",
    },
}

# "Reschedule" tapped inside the change-notice period: {when}, {hours}, then staff hours.
LATE_RESCHEDULE = {
    "ar": "موعدك {when} باقي عليه أقل من {hours} ساعة، وحسب سياستنا ما نقدر نغيره من هنا. "
    "حولت طلبك للفريق ويتواصلون معك.",
    "en": "Your appointment on {when} is less than {hours} hours away, so under our policy it "
    "can't be changed here. I've passed your request to our team, who will get back to you.",
}

# What a "Reschedule" tap on a reminder means, handed to the assistant as the
# customer's words. {when} is the booking's day and time.
RESCHEDULE_REQUEST = {
    "ar": "أبي أغير موعدي اللي {when}",
    "en": "I'd like to reschedule my appointment on {when}.",
}


class InboundHandler:
    """Handles ``handle_inbound`` jobs. Register ``handle`` with the worker."""

    def __init__(
        self,
        settings: Settings,
        client: WhatsAppClient,
        replier: Replier,
        clock: Callable[[], datetime] | None = None,
        kb: KnowledgeBase | None = None,
    ) -> None:
        """
        Args:
            settings: Runtime config.
            client: Sends replies (always inside the 24h window).
            replier: Produces the bot's reply (``AgentReplier`` in production).
            clock: Returns "now" (UTC) for alert timestamps; evals pin it.
            kb: For the booking rules (change notice) on reminder replies.
                Without it, every Reschedule tap goes to the assistant.
        """
        self._settings = settings
        self._client = client
        self._replier = replier
        self._clock = clock or (lambda: datetime.now(UTC))
        self._kb = kb

    def handle(self, session: Session, payload: dict[str, Any]) -> None:
        """Handle one inbound message job.

        Args:
            session: Open session. The worker commits.
            payload: Job payload from ``webhook.service`` (conversation_id,
                wa_message_id, type, reply_id).

        Failure modes:
            * ``OutsideServiceWindowError`` is logged and swallowed.
            * ``ExternalServiceError`` propagates, so the queue retries with backoff.
        """
        conv = session.get(Conversation, int(payload["conversation_id"]))
        if conv is None:
            log.warning("conversation vanished", extra={"event": "job_skip"})
            return
        customer = session.get(Customer, conv.customer_id)
        if customer is None:
            log.warning("customer vanished", extra={"conversation_id": conv.id})
            return
        inbound = session.execute(
            select(Message).where(Message.wa_message_id == payload["wa_message_id"])
        ).scalar_one_or_none()
        text = inbound.body if inbound else None
        if inbound is not None and self._superseded(session, conv, inbound):
            log.info(
                "stale message not answered", extra={"conversation_id": conv.id, "event": "stale"}
            )
            self._handle_opt_out(
                session, conv, customer, text, customer.language or "ar", reply=False
            )
            return

        lang = detect_language(text, fallback=customer.language or self._settings.default_language)
        if text:
            customer.language = lang

        # Opt-outs first, in every state: they can't wait for staff (§4).
        if self._handle_opt_out(session, conv, customer, text, lang):
            return

        reply_id = str(payload["reply_id"]) if payload.get("reply_id") else None
        tap = self._reminder_tap(session, customer, reply_id)
        if tap is not None:
            booking, action = tap
            if conv.state is ConversationState.HUMAN:
                self._release(conv)  # §5: a reminder reply opens a new turn in BOT state
            if action == reminders.CONFIRM:
                self._send(session, conv, customer, Reply(CANNED["reminder_confirmed"][lang]))
                return
            start, tz = as_utc(booking.start_at), self._settings.tz
            at = "الساعة" if lang == "ar" else "at"
            when = f"{day_label(start, tz, lang)} {at} {time_label(start, tz)}"
            if self._too_late_to_change(booking):
                self._late_reschedule(session, conv, customer, booking, when, lang)
                return
            # The id saves the model a lookup (live testing: it guessed an id, got
            # not_found, then listed the bookings: one wasted call per tap).
            request = RESCHEDULE_REQUEST[lang].format(when=when)
            text = f"{request}\n[Tapped Reschedule on the reminder for booking_id {booking.id}]"
            reply_id = None

        if conv.state is ConversationState.HUMAN and not self._maybe_auto_release(conv):
            return  # a human owns this conversation, so the bot stays silent (§5)

        if payload.get("type") == "unsupported":
            self._send(session, conv, customer, Reply(CANNED["unsupported_media"][lang]))
            return

        reply = self._replier.reply(
            ReplyContext(
                session=session,
                conversation=conv,
                customer=customer,
                language=lang,
                text=text,
                reply_id=reply_id,
                inbound_message_id=inbound.id if inbound else None,
            )
        )
        if reply and reply.text:
            self._send(session, conv, customer, reply)
        if conv.state is ConversationState.HUMAN:
            # HUMAN conversations returned above, so the assistant handed off
            # during this turn: wake the staff up (§5).
            queue_handoff_alerts(session, self._settings, conv, self._clock())

    # ------------------------------------------------------------ opt-out

    @staticmethod
    def _superseded(session: Session, conv: Conversation, inbound: Message) -> bool:
        """True if the customer sent a newer message than ``inbound`` (out-of-order delivery)."""
        newer = session.execute(
            select(Message.id).where(
                Message.conversation_id == conv.id,
                Message.direction == Direction.INBOUND,
                Message.created_at > inbound.created_at,
            )
        ).first()
        return newer is not None

    def _handle_opt_out(
        self,
        session: Session,
        conv: Conversation,
        customer: Customer,
        text: str | None,
        lang: str,
        *,
        reply: bool = True,
    ) -> bool:
        """Record an opt-out and confirm it once. Returns True if the message was one."""
        kind = classify(text)
        if kind is Classification.CANCEL_WORD:
            if not self._replying_to_followup(session, conv):
                return False  # "cancel" = cancel my booking: the assistant handles it
            kind = Classification.FOLLOWUPS
        if kind is Classification.NONE:
            return False
        if kind is Classification.FOLLOWUPS:
            first_time = not customer.opted_out_followups
            customer.opted_out_followups = True
            followups.cancel_pending_for(session, customer.id)
            canned = "opt_out_followups"
        else:
            first_time = not customer.opted_out_reminders
            customer.opted_out_reminders = True
            for booking_id in session.execute(
                select(Booking.id).where(Booking.customer_id == customer.id)
            ).scalars():
                reminders.cancel_pending(session, booking_id)
            canned = "opt_out_reminders"
        log.info("opt-out recorded", extra={"conversation_id": conv.id, "event": canned})
        if first_time and reply:  # §4: confirm the opt-out once
            self._send(session, conv, customer, Reply(CANNED[canned][lang]))
        return True

    @staticmethod
    def _replying_to_followup(session: Session, conv: Conversation) -> bool:
        """True if the last message we sent in this conversation was a lead follow-up."""
        last = session.execute(
            select(Message.wa_message_id)
            .where(Message.conversation_id == conv.id, Message.direction == Direction.OUTBOUND)
            .order_by(Message.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if last is None:
            return False
        followup = session.execute(select(FollowUp.id).where(FollowUp.wa_message_id == last))
        return followup.first() is not None

    # ------------------------------------------------------------ reminders

    @staticmethod
    def _reminder_tap(
        session: Session, customer: Customer, reply_id: str | None
    ) -> tuple[Booking, str] | None:
        """The booking and action behind a reminder quick reply, if it is this customer's."""
        parsed = reminders.parse_payload(reply_id)
        if parsed is None:
            return None
        reminder = session.get(Reminder, parsed[0])
        booking = session.get(Booking, reminder.booking_id) if reminder else None
        if (
            booking is None
            or booking.customer_id != customer.id
            or booking.status is not BookingStatus.CONFIRMED
        ):
            return None  # stale or foreign button: treat the tap as plain text
        return booking, parsed[1]

    def _too_late_to_change(self, booking: Booking) -> bool:
        """True if the booking rules forbid moving ``booking`` now (change-notice period)."""
        if self._kb is None:
            return False
        try:
            check_change_allowed(self._kb, booking, self._clock())
        except BookingError as exc:
            return exc.code == "too_late_to_change"
        return False

    def _late_reschedule(
        self,
        session: Session,
        conv: Conversation,
        customer: Customer,
        booking: Booking,
        when: str,
        lang: str,
    ) -> None:
        """State the policy, hand the chat to staff, and alert them (found in live testing:
        the model hit ``too_late_to_change`` and handed off without saying why)."""
        assert self._kb is not None
        hours = f"{self._kb.rules.min_cancel_notice_hours:g}"
        text = LATE_RESCHEDULE[lang].format(when=when, hours=hours)
        text = f"{text} {self._settings.handoff_hours_for(lang)}".strip()
        hand_off(
            conv,
            "late_change",
            f"Tapped Reschedule on the reminder for booking {booking.id} ({when}), inside the "
            f"{hours}h change-notice period. Wants to move it.",
        )
        self._send(session, conv, customer, Reply(text))
        queue_handoff_alerts(session, self._settings, conv, self._clock())

    @staticmethod
    def _release(conv: Conversation) -> None:
        conv.state = ConversationState.BOT
        conv.handed_off_at = None
        conv.handoff_reason = None
        conv.handoff_summary = None
        conv.unread_for_staff = 0

    def _maybe_auto_release(self, conv: Conversation) -> bool:
        """Return the conversation to BOT once HANDOFF_AUTO_RELEASE_HOURS has passed.

        This stops a conversation from staying silent forever when staff never
        pick it up.
        """
        if conv.handed_off_at is None:
            return False
        handed = conv.handed_off_at
        handed = handed if handed.tzinfo else handed.replace(tzinfo=UTC)  # SQLite returns naive
        limit = timedelta(hours=self._settings.handoff_auto_release_hours)
        if datetime.now(UTC) - handed < limit:
            return False
        conv.state = ConversationState.BOT
        conv.handed_off_at = None
        conv.handoff_reason = None
        conv.handoff_summary = None
        conv.unread_for_staff = 0
        log.info("handoff auto-released", extra={"conversation_id": conv.id})
        return True

    def _send(self, session: Session, conv: Conversation, customer: Customer, reply: Reply) -> None:
        window = conv.last_customer_msg_at
        try:
            if reply.buttons:
                wamid = self._client.send_buttons(
                    customer.wa_id, reply.text, list(reply.buttons), last_customer_msg_at=window
                )
            elif reply.rows:
                wamid = self._client.send_list(
                    customer.wa_id,
                    reply.text,
                    reply.button,
                    list(reply.rows),
                    last_customer_msg_at=window,
                )
            else:
                wamid = self._client.send_text(
                    customer.wa_id, reply.text, last_customer_msg_at=window
                )
        except OutsideServiceWindowError:
            log.warning("reply skipped: 24h window closed", extra={"conversation_id": conv.id})
            return
        session.add(
            Message(
                conversation_id=conv.id,
                direction=Direction.OUTBOUND,
                sender=Sender.BOT,
                wa_message_id=wamid,
                type="interactive" if reply.rows or reply.buttons else "text",
                body=reply.text,
                status=MessageStatus.PENDING,
            )
        )
