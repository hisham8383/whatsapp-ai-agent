"""``AgentReplier``: the LLM with booking and handoff tools (CLAUDE.md §5).

One customer message = one turn:

1. Build the history from the stored conversation (last
   ``LLM_MAX_HISTORY_MESSAGES`` text messages; staff replies included).
2. Prefix the new message with a ``[context]`` line: current local time,
   customer name, reply language. It stays out of the system prompt so the
   prompt remains cacheable.
3. Loop: model step → run its tool calls → feed results back, until the model
   answers in text (at most ``MAX_STEPS`` steps).
4. Build the reply:
     * after ``handoff_to_human``: the fixed handoff message + staff hours,
       written by code so the wording is always right;
     * when a booking tool asks for confirmation: the model is stopped and a
       code-written summary with "Confirm / Change" buttons is sent
       (``app/conversation/messages.py``);
     * after a booking change ran: a code-written "done" message;
     * after ``get_availability`` returned times: the model's text as an
       interactive list of up to 10 slots;
     * otherwise the model's text.

Tapping **Confirm** runs the pending change directly, without the model. If
it fails (say the slot was just taken), the model is told why and takes over.

Two slips are corrected with one extra model step (a system note), because
the eval set caught Haiku making them now and then:
    * claiming "booked" / "cancelled" when no booking tool ran;
    * asking "shall I cancel it?" in text after looking the booking up,
      instead of calling the tool (the customer would then confirm twice).
If the retry still claims a change that didn't happen, the claim is never
sent: the customer gets the neutral failure text instead.

Failures degrade instead of going silent. If the LLM fails (or loops), the
customer gets a short "please try again", and the second consecutive failure
hands the conversation to staff (§5: "the model fails twice in a row").
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select

from app.booking.base import BookingProvider
from app.config import Settings
from app.conversation import messages
from app.conversation.prompt import build_system_prompt
from app.conversation.replier import Reply, ReplyContext
from app.conversation.timefmt import MODEL_FORMAT, context_now, slot_label
from app.conversation.tools import TOOLS, ToolRunner, TurnState, hand_off, names_a_time
from app.db.models import Message, Sender
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase
from app.llm.base import LLMClient, ToolResult, Turn
from app.logging import get_logger
from app.whatsapp.client import MAX_LIST_ROWS, ListRow

log = get_logger(__name__)

MAX_STEPS = 6  # model calls per customer message; a normal booking step needs 2-3
# Tools that only record something and never change the answer. When the model
# writes its reply and calls only these in the same step, the reply is final:
# no extra model call (fewer tokens, §2), and no risk of the empty follow-up
# turn Haiku tends to produce after them.
SILENT_TOOLS = frozenset({"mark_lead_interest"})
SLOT_PREFIX = "slot|"

# Put before the handoff message when the turn's change was refused by the
# change-notice rule, so the customer knows why (found in live testing).
TOO_LATE_TEXT = {
    "ar": "حسب سياستنا ما نقدر نغير أو نلغي الموعد قبل أقل من {hours} ساعة من وقته.",
    "en": "Under our policy, appointments can't be changed or cancelled less than "
    "{hours} hours before they start.",
}
HANDOFF_TEXT = {
    "ar": "حوّلت محادثتك لفريقنا، وبيرد عليك أحد الموظفين هنا في أقرب وقت.",
    "en": "I've passed your conversation to our team; a team member will reply here soon.",
}
FAILURE_TEXT = {
    "ar": "عذرًا، صار عندنا خلل مؤقت. ممكن ترسل رسالتك مرة ثانية بعد قليل؟",
    "en": "Sorry, something went wrong on our side. Could you send your message again in a moment?",
}
LIST_BUTTON = {"ar": "المواعيد المتاحة", "en": "Available times"}

# Words that mean "it's done". If the model says one of these while a change is
# still waiting for confirmation and nothing ran, the reply is not sent: the
# confirmation is asked again instead (a false "you're booked" is the worst
# failure a booking bot can have).
_DONE_CLAIM = re.compile(
    r"\b(booked|confirmed|cancel+ed|rescheduled|all set)\b"
    r"|تم\s*(ال)?(حجز|تأكيد|إلغاء|الغاء|تغيير|تعديل)|تم تأكيده|حجزت لك|ألغيت"
    r"|أكدت (لك )?(الحجز|الموعد)",
    re.IGNORECASE,
)


# A claim that the customer's change is done: stricter than _DONE_CLAIM, so
# "Monday is fully booked" (a true statement about availability) doesn't match.
_CLAIMS_CHANGE = re.compile(
    r"\b(you'?re|you are|i'?ve|i have|we'?ve|we have|has been|have been|is now|it'?s)\s+"
    r"(now\s+|all\s+)?(booked|confirmed|cancel+ed|rescheduled|moved|set)\b"
    r"|\b(booked|confirmed) you\b|\bi (booked|cancel+ed|moved|rescheduled)\b"
    r"|تم\s*(ال)?(حجز|تأكيد|إلغاء|الغاء|تغيير|تعديل)|حجزت لك|ألغيت|أكدت (لك )?(الحجز|الموعد)",
    re.IGNORECASE,
)

# "Shall I cancel it?" in words, which should have been a tool call (buttons follow).
_ASKS_TO_CHANGE = re.compile(
    r"\b(shall|should) i\b.{0,40}\b(book|cancel|move|reschedule|go ahead)"
    r"|\b(do you want|would you like) me to\b.{0,30}\b(book|cancel|move|reschedule)"
    r"|(تبي|تبغى|تبين|تبغين|ودك)\s+(ا|أ)?(لغي|الغي|ألغي|أحجز|احجز|اغير|أغير|تلغي|نلغي|نحجز|نغير)"
    # "move it" (the appointment): تحوله/تحوليه/ننقلها… but not "أحولك" (transfer you to staff)
    r"|(تبي|تبغى|تبين|تبغين|ودك)\s+(ا|أ|ت|ن)?(حول|نقل)(ي)?(ه|ها)(?!\w)",
    re.IGNORECASE,
)
FALSE_CLAIM_NOTE = (
    "[System: your last reply said the appointment is booked, moved or cancelled, but "
    "nothing has changed. Never say that yourself. If the customer has chosen a time or "
    "asked for the change, call the booking tool now: the system then shows the details "
    "with Confirm / Change buttons. Otherwise answer without claiming anything is done.]"
)
ASKED_IN_TEXT_NOTE = (
    "[System: don't ask the customer in words whether to book, move or cancel. Call "
    "create_booking, reschedule_booking or cancel_booking now: the system shows the "
    "details with Confirm / Change buttons.]"
)


class AgentReplier:
    """Implements ``Replier`` with the LLM tool loop. One instance per worker process."""

    def __init__(
        self,
        settings: Settings,
        kb: KnowledgeBase,
        llm: LLMClient,
        provider: BookingProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """
        Args:
            settings: Runtime config.
            kb: Validated knowledge base.
            llm: Provider adapter.
            provider: Booking adapter.
            clock: Returns "now" (UTC) for booking rules and the context line.
                The eval set pins it; production uses the real clock.
        """
        self._settings = settings
        self._kb = kb
        self._llm = llm
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._system = build_system_prompt(settings, kb)  # built once, stays byte-stable

    # ------------------------------------------------------------ history

    def _history(self, ctx: ReplyContext) -> list[Turn]:
        """Stored text messages before this one, oldest first, as model turns.

        Staff replies become assistant turns marked as staff, so the model knows
        what a person already told the customer. Consecutive same-role messages
        are merged, and the history always starts with a customer message.
        """
        stmt = (
            select(Message.sender, Message.body)
            .where(Message.conversation_id == ctx.conversation.id, Message.body.is_not(None))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(self._settings.llm_max_history_messages + 1)
        )
        if ctx.inbound_message_id is not None:
            stmt = stmt.where(Message.id != ctx.inbound_message_id)
        rows = list(reversed(ctx.session.execute(stmt).all()))[
            -self._settings.llm_max_history_messages :
        ]
        turns: list[tuple[Literal["user", "assistant"], str]] = []
        for sender, body in rows:
            role: Literal["user", "assistant"] = (
                "user" if sender is Sender.CUSTOMER else "assistant"
            )
            text = f"[Staff member wrote]: {body}" if sender is Sender.STAFF else str(body)
            if turns and turns[-1][0] == role:
                turns[-1] = (role, f"{turns[-1][1]}\n\n{text}")
            else:
                turns.append((role, text))
        while turns and turns[0][0] == "assistant":
            turns.pop(0)
        return [Turn.text(role, text) for role, text in turns]

    def _user_text(self, ctx: ReplyContext, now: datetime, note: str = "") -> str:
        tz = self._settings.tz
        context = (
            f"[context: now={context_now(now, tz)} ({self._settings.business_timezone}); "
            f"customer_name={ctx.customer.name or 'unknown'}; reply_language={ctx.language}]"
        )
        text = (ctx.text or "").strip()
        if ctx.reply_id and ctx.reply_id.startswith(SLOT_PREFIX):
            parts = ctx.reply_id.split("|")
            if len(parts) == 4:
                _, service_id, start, staff = parts
                text = (
                    f"{text}\n[Picked from the list: service_id={service_id}, "
                    f"start={start.replace('T', ' ')}, staff_id={staff or 'any'}]"
                )
        if note:
            text = f"{text}\n{note}"
        return f"{context}\n{text}"

    # ------------------------------------------------------------ turn

    def reply(self, ctx: ReplyContext) -> Reply | None:
        """Produce the reply for one customer message. See the module docstring.

        Failure modes:
            * LLM errors or too many steps -> ``FAILURE_TEXT``; a second
              consecutive failure hands off to staff.
            * Tool failures are returned to the model as error results.
        """
        conv = ctx.conversation
        now = self._clock()
        state = TurnState(
            now=now,
            prev_pending=conv.pending_action,
            customer_chose_time=bool(ctx.reply_id) or names_a_time(ctx.text),
        )
        conv.pending_action = None  # a proposal lives for exactly one customer message
        runner = ToolRunner(
            self._settings, self._kb, self._provider, ctx.session, conv, ctx.customer, state
        )
        note = ""
        if ctx.reply_id == messages.CONFIRM_ID:
            if state.prev_pending is None:
                note = (
                    "[The customer tapped Confirm on an older message, but nothing is waiting "
                    "for confirmation any more. Ask what they would like to do.]"
                )
            else:
                outcome = runner.execute_pending(state.prev_pending)
                if state.done is not None:
                    conv.bot_failures = 0
                    return messages.done(state.done, self._kb, self._settings.tz, ctx.language)
                note = (
                    "[The customer tapped Confirm, but the change failed: "
                    f"{outcome.content}. Explain simply and offer another option.]"
                )
        elif ctx.reply_id == messages.CHANGE_ID:
            note = (
                "[The customer tapped the second button: they don't want the change as proposed. "
                "Ask what they would like instead.]"
            )
        elif state.prev_pending is not None:
            pending = state.prev_pending
            args = {
                **pending.get("args", {}),
                **{k: v for k, v in pending.get("extra", {}).items() if v},
            }
            note = (
                "[The customer was just asked to confirm this change: "
                f"{pending.get('action')} {json.dumps(args, ensure_ascii=False)}. If this message "
                "agrees, call that tool again with exactly these arguments. If not, don't.]"
            )
        history = [*self._history(ctx), Turn.text("user", self._user_text(ctx, now, note))]
        log_extra = {"conversation_id": conv.id}
        started = time.monotonic()
        text = ""
        last_text = ""  # text the model wrote next to tool calls, kept as a fallback
        corrected = claim_corrected = False
        picked = (ctx.reply_id or "").startswith(SLOT_PREFIX)
        try:
            for step in range(MAX_STEPS + 1):
                if step == MAX_STEPS:
                    raise ExternalServiceError("llm", f"no final answer after {MAX_STEPS} steps")
                try:
                    result = self._llm.complete(self._system, history, TOOLS)
                except ExternalServiceError as exc:
                    if last_text and "empty reply" in str(exc):
                        # The model already answered alongside its tool call.
                        text = last_text
                        break
                    raise
                log.info(
                    "llm step",
                    extra={
                        **log_extra,
                        "event": "llm_step",
                        "step": step,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "cache_read_tokens": result.cache_read_tokens,
                        "cache_write_tokens": result.cache_write_tokens,
                        "status": result.stop_reason,
                    },
                )
                calls = result.tool_calls
                if not calls:
                    text = result.text
                    note = "" if corrected else self._correction(state, text, picked)
                    if not note:
                        break
                    corrected = True  # one extra step at most
                    claim_corrected = note == FALSE_CLAIM_NOTE
                    log.warning("correcting reply", extra={**log_extra, "event": "correction"})
                    history += [Turn.text("assistant", text), Turn.text("user", note)]
                    continue
                results: list[ToolResult] = [runner.run(c) for c in calls]
                last_text = result.text or last_text
                if result.text and all(c.name in SILENT_TOOLS for c in calls):
                    text = result.text
                    break
                history += [
                    Turn("assistant", tuple(result.blocks)),
                    Turn("user", tuple(results)),
                ]
                if state.handed_off or state.proposal is not None:
                    break  # code writes the reply from here (handoff / confirmation)
        except ExternalServiceError as exc:
            log.error("agent turn failed: %s", exc, extra={**log_extra, "event": "llm_failed"})
            return self._failed(ctx)
        finally:
            log.info(
                "agent turn",
                extra={**log_extra, "duration_ms": round((time.monotonic() - started) * 1000)},
            )

        conv.bot_failures = 0
        if state.handed_off:
            # The policy is stated whenever the change was refused, or the model
            # handed off right after seeing an unchangeable booking (eval set).
            too_late = state.too_late or state.unchangeable_listed
            return self._handoff_reply(ctx.language, too_late=too_late)
        conv.pending_action = state.new_pending
        tz = self._settings.tz
        if state.proposal is not None:
            return messages.confirmation(state.proposal, self._kb, tz, ctx.language)
        if state.done is not None:
            return messages.done(state.done, self._kb, tz, ctx.language)
        if state.prev_pending is not None and _DONE_CLAIM.search(text):
            log.warning(
                "model claimed a change that didn't run; asking to confirm again",
                extra={**log_extra, "event": "false_done_claim"},
            )
            runner.propose_again(state.prev_pending)
            if state.proposal is not None:
                conv.pending_action = state.new_pending
                return messages.confirmation(state.proposal, self._kb, tz, ctx.language)
            return Reply(FAILURE_TEXT[ctx.language])
        if claim_corrected and _CLAIMS_CHANGE.search(text):
            # Still claiming a change after the correction: never send a false "booked".
            log.warning("false done claim not sent", extra={**log_extra, "event": "false_done"})
            text = ""
        if not text:
            text = FAILURE_TEXT[ctx.language]
        if state.offered:
            return Reply(text[:1024], self._rows(state, ctx.language), LIST_BUTTON[ctx.language])
        return Reply(text)

    @staticmethod
    def _correction(state: TurnState, text: str, picked: bool) -> str:
        """The system note for one corrective step, or "" if the reply is fine.

        The "claimed a change" check only runs after a time lookup or a slot
        pick, where a past-tense "booked" can't be true; a customer asking
        whether an existing booking is confirmed gets a normal answer.
        """
        if state.done is not None or state.proposal is not None or state.handed_off:
            return ""
        if (
            state.prev_pending is None
            and (state.looked_up or picked)
            and _CLAIMS_CHANGE.search(text)
        ):
            return FALSE_CLAIM_NOTE
        if (state.listed or state.looked_up) and _ASKS_TO_CHANGE.search(text):
            return ASKED_IN_TEXT_NOTE
        return ""

    def _failed(self, ctx: ReplyContext) -> Reply:
        conv = ctx.conversation
        conv.bot_failures += 1
        if conv.bot_failures >= 2:
            last = (ctx.text or "")[:300]
            hand_off(
                conv,
                "repeated_failure",
                f"Automatic handoff: the assistant failed twice in a row. Last message: {last}",
            )
            conv.bot_failures = 0
            return self._handoff_reply(ctx.language)
        return Reply(FAILURE_TEXT[ctx.language])

    def _handoff_reply(self, language: str, *, too_late: bool = False) -> Reply:
        text = f"{HANDOFF_TEXT[language]} {self._settings.handoff_hours_for(language)}".strip()
        if too_late:
            hours = f"{self._kb.rules.min_cancel_notice_hours:g}"
            text = f"{TOO_LATE_TEXT[language].format(hours=hours)} {text}"
        return Reply(text)

    def _rows(self, state: TurnState, language: str) -> tuple[ListRow, ...]:
        service = state.offered_service
        assert service is not None
        tz = self._settings.tz
        rows: list[ListRow] = []
        for slot in state.offered[:MAX_LIST_ROWS]:
            staff = self._kb.staff_member(slot.staff_id) if slot.staff_id else None
            local = slot.start.astimezone(tz).strftime(MODEL_FORMAT).replace(" ", "T")
            rows.append(
                ListRow(
                    id=f"{SLOT_PREFIX}{service.id}|{local}|{slot.staff_id or ''}",
                    title=slot_label(slot.start, tz, language),
                    description=(staff.name_ar if language == "ar" else staff.name_en)
                    if staff
                    else None,
                )
            )
        return tuple(rows)
