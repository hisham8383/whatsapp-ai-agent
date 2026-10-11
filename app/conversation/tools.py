"""The LLM's tools and the code that runs them (CLAUDE.md §5).

| Tool                     | Effect                                             |
|--------------------------|----------------------------------------------------|
| get_availability         | free slots from the booking provider               |
| create_booking           | book, after the customer confirmed                 |
| list_customer_bookings   | upcoming bookings for this WhatsApp number         |
| reschedule_booking       | move a booking, after the customer confirmed       |
| cancel_booking           | cancel a booking, after the customer confirmed     |
| handoff_to_human         | set state HUMAN and store a summary for staff      |
| mark_lead_interest       | record interest, so one follow-up can go out later |

Every booking change also re-plans that booking's reminders
(``app/scheduler/reminders.py``) in the same transaction, so a booking and
its reminders can never disagree (§5).

Confirmation guard (enforced in code, not only in the prompt). A booking
change runs only if the *previous* bot turn proposed exactly the same change.
The first call records a ``pending_action`` on the conversation and returns
``needs_confirmation``. The agent then stops the model and sends a
confirmation message written by code, with "Confirm / Change" buttons
(``app/conversation/messages.py``), so the model can't word it as if the
booking were already made. The pending action lives for one customer message:
tapping Confirm runs it; typing "yes" leads the model to repeat the same call,
which runs it; anything else discards it. So nothing is ever booked, moved, or
cancelled on the same turn it was first proposed.

All tool results are short English JSON for the model. Nothing here writes to
the customer directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.booking.base import BookingError, BookingProvider, Slot, as_utc, check_change_allowed
from app.config import Settings
from app.conversation.timefmt import for_model, parse_day, parse_local
from app.db.models import (
    Booking,
    BookingStatus,
    Conversation,
    ConversationState,
    Customer,
    utcnow,
)
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase, Service
from app.llm.base import ToolCall, ToolResult, ToolSpec
from app.logging import get_logger
from app.scheduler import followups, reminders

log = get_logger(__name__)

# Longest range one availability call may cover, so answers stay small.
MAX_RANGE_DAYS = 14
# Slots listed back to the model. The customer's list shows at most 10.
MAX_SLOTS_TO_MODEL = 20

_DATE = {"type": "string", "description": "Local date, YYYY-MM-DD."}
_START = {"type": "string", "description": "Local date and time, 'YYYY-MM-DD HH:MM'."}

TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_availability",
        "Free appointment times for one service between two local dates (inclusive, at "
        "most 14 days). Call it before offering or accepting any time.",
        {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "date_from": _DATE,
                "date_to": _DATE,
                "staff_id": {
                    "type": "string",
                    "description": "Only if the customer asked for a specific staff member.",
                },
                "preferred_start": {
                    "type": "string",
                    "description": "The exact time the customer asked for, 'YYYY-MM-DD HH:MM', "
                    "if they named one.",
                },
            },
            "required": ["service_id", "date_from", "date_to"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "create_booking",
        "Propose a booking. The first call books nothing: the system shows the customer the "
        "details with Confirm / Change buttons. Call again with identical details only if the "
        "customer then agrees in words.",
        {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "start": _START,
                "customer_name": {"type": "string"},
                "staff_id": {"type": "string"},
                "notes": {"type": "string", "description": "Optional, non-sensitive."},
            },
            "required": ["service_id", "start", "customer_name"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "list_customer_bookings",
        "This customer's upcoming appointments, with booking_id and can_change (false = "
        "too close to the appointment to change or cancel under the booking rules).",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolSpec(
        "reschedule_booking",
        "Move an appointment to a new time. First call returns needs_confirmation; call "
        "again with identical details after the customer confirms.",
        {
            "type": "object",
            "properties": {"booking_id": {"type": "integer"}, "new_start": _START},
            "required": ["booking_id", "new_start"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "cancel_booking",
        "Cancel an appointment. First call returns needs_confirmation; call again with "
        "identical details after the customer confirms.",
        {
            "type": "object",
            "properties": {
                "booking_id": {"type": "integer"},
                "reason": {"type": "string", "description": "Optional, short."},
            },
            "required": ["booking_id"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "handoff_to_human",
        "Hand the conversation to staff. The assistant then stays silent until staff hand it back.",
        {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [
                        "asked_for_human",
                        "complaint",
                        "out_of_scope",
                        "discount_or_exception",
                        "repeated_failure",
                        "late_change",
                        "other",
                    ],
                },
                "summary": {
                    "type": "string",
                    "description": "1-2 English sentences for staff: what the customer needs.",
                },
            },
            "required": ["reason", "summary"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "mark_lead_interest",
        "Silently note that the customer is interested in a service (asked its price, "
        "details or availability) but hasn't booked it. The business may follow up once "
        "later. Never mention this to the customer.",
        {
            "type": "object",
            "properties": {"service_id": {"type": "string"}},
            "required": ["service_id"],
            "additionalProperties": False,
        },
    ),
]

BOOKING_CHANGE_TOOLS = {"create_booking", "reschedule_booking", "cancel_booking"}

# Does a customer message name a time? Digits (Western or Arabic-Indic), or an
# hour in words. Used to stop the model from choosing a time on the customer's
# behalf right after looking the times up (§4: slots are picked from a list).
# Arabic hour words are matched as whole words, so day names that contain them
# (الاثنين Monday, الثلاثاء Tuesday, الخميس Thursday, الأسبوع week) don't count.
_NAMES_TIME = re.compile(
    r"[0-9٠-٩]"
    r"|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|noon|midday)\b"
    r"|(?<!\w)(واحدة|وحدة|ثنتين|اثنين|ثلاث|ثلاثة|اربع|أربع|اربعة|أربعة|خمس|خمسة|ست|ستة"
    r"|سبع|سبعة|ثمان|ثمانية|تسع|تسعة|عشر|عشرة|الواحدة|الثانية|الثالثة|الرابعة|الخامسة"
    r"|السادسة|السابعة|الثامنة|التاسعة|العاشرة|الظهر|الظهيرة)(?!\w)",
    re.IGNORECASE,
)


def names_a_time(text: str | None) -> bool:
    """True if the customer's message mentions a clock time in any common form."""
    return bool(text and _NAMES_TIME.search(text))


@dataclass(frozen=True, slots=True)
class ChangeInfo:
    """A booking change, proposed or done, with what the customer-facing message needs."""

    action: str  # create_booking | reschedule_booking | cancel_booking
    service: Service
    start: datetime  # UTC: the booking's time (the new time for a reschedule)
    staff_id: str | None
    old_start: datetime | None = None  # reschedule only


@dataclass
class TurnState:
    """What the tools did during one customer message."""

    now: datetime  # clock for booking rules (pinned in evals)
    prev_pending: dict[str, Any] | None  # proposed on the previous turn
    new_pending: dict[str, Any] | None = None  # proposed on this turn
    consumed: bool = False  # prev_pending was executed this turn
    offered_service: Service | None = None
    offered: list[Slot] = field(default_factory=list)  # last non-empty availability answer
    handed_off: bool = False
    changed_booking: bool = False
    customer_chose_time: bool = True  # this message names a time or picks a slot
    looked_up: bool = False  # get_availability ran during this message
    listed: bool = False  # list_customer_bookings ran during this message
    too_late: bool = False  # a change was refused inside the change-notice period
    unchangeable_listed: bool = False  # a listed booking is inside the notice period
    proposal: ChangeInfo | None = None  # needs the customer's confirmation (stops the model)
    done: ChangeInfo | None = None  # the change that ran this turn


def _ok(**data: Any) -> str:
    return json.dumps({"status": "done", **data}, ensure_ascii=False)


def _err(code: str, message: str) -> str:
    return json.dumps({"status": "error", "error": code, "message": message}, ensure_ascii=False)


class ToolRunner:
    """Runs the model's tool calls for one conversation turn."""

    def __init__(
        self,
        settings: Settings,
        kb: KnowledgeBase,
        provider: BookingProvider,
        session: Session,
        conversation: Conversation,
        customer: Customer,
        state: TurnState,
    ) -> None:
        self._settings = settings
        self._tz = settings.tz
        self._kb = kb
        self._provider = provider
        self._session = session
        self._conv = conversation
        self._customer = customer
        self.state = state

    # ------------------------------------------------------------ dispatch

    def run(self, call: ToolCall) -> ToolResult:
        """Run one tool call. Never raises: every failure becomes an error result.

        Failure modes:
            * Bad arguments or a broken booking rule -> ``is_error`` result the
              model can explain to the customer.
            * Booking system unreachable (``ExternalServiceError``) -> error
              result ``booking_system_unavailable``; the model offers a handoff.
        """
        handler = getattr(self, f"_tool_{call.name}", None)
        if handler is None:
            return ToolResult(call.id, _err("unknown_tool", call.name), is_error=True)
        try:
            content: str = handler(call.input)
        except BookingError as exc:
            content = _err(exc.code, exc.detail or exc.code.replace("_", " "))
            if exc.code == "too_late_to_change":
                self.state.too_late = True
        except ExternalServiceError as exc:
            log.error("booking provider failed: %s", exc, extra={"conversation_id": self._conv.id})
            content = _err(
                "booking_system_unavailable",
                "The booking system isn't responding. Apologize and offer to connect the "
                "customer with the team.",
            )
        except (KeyError, TypeError, ValueError) as exc:
            content = _err("bad_arguments", str(exc))
        is_error = '"status": "error"' in content
        log.info(
            "tool %s", call.name, extra={"conversation_id": self._conv.id, "status": content[:40]}
        )
        return ToolResult(call.id, content, is_error=is_error)

    # ------------------------------------------------------------ helpers

    def _service(self, service_id: Any) -> Service:
        svc = self._kb.service(str(service_id))
        if svc is None:
            ids = ", ".join(s.id for s in self._kb.services)
            raise BookingError("unknown_service", f"use one of: {ids}")
        return svc

    def _staff_id(self, value: Any) -> str | None:
        if value in (None, "", "any"):
            return None
        staff = str(value)
        if self._kb.staff_member(staff) is None:
            raise BookingError("unknown_staff", f"no staff_id {staff}")
        return staff

    def _staff_name(self, staff_id: str | None) -> str | None:
        st = self._kb.staff_member(staff_id) if staff_id else None
        return f"{st.name_en} / {st.name_ar}" if st else None

    def _own_booking(self, booking_id: Any) -> Booking:
        booking = self._session.get(Booking, int(booking_id))
        if booking is None or booking.customer_id != self._customer.id:
            raise BookingError("not_found", "no such booking for this customer")
        if booking.status is not BookingStatus.CONFIRMED:
            raise BookingError("not_active", f"booking is {booking.status.value}")
        return booking

    def _describe(self, service: Service, start: datetime, staff_id: str | None) -> dict[str, Any]:
        local = start.astimezone(self._tz)
        return {
            "service": f"{service.name_en} / {service.name_ar}",
            "weekday": local.strftime("%A"),
            "start": for_model(start, self._tz),
            "duration_minutes": service.duration_minutes,
            **({"price_sar": service.price_sar} if service.price_sar is not None else {}),
            **({"staff": name} if (name := self._staff_name(staff_id)) else {}),
        }

    def _require_offered(self, service: Service, start: datetime, staff_id: str | None) -> Slot:
        """The slot must be one ``get_availability`` returns right now (§5: never invent times)."""
        day = start.astimezone(self._tz).date()
        slots = self._provider.get_availability(
            self._session, service, day, day, staff_id, self.state.now
        )
        match = next((s for s in slots if s.start == start), None)
        if match is None:
            raise BookingError(
                "slot_unavailable",
                "that time isn't free or isn't bookable; call get_availability and offer "
                "the times it returns",
            )
        return match

    def _confirmed(
        self,
        info: ChangeInfo,
        args: dict[str, Any],
        details: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> str | None:
        """The confirmation guard. Returns ``None`` to proceed, or the result to send instead.

        Args:
            info: The change, for the confirmation message.
            args: Normalized arguments that identify the change. The change runs
                only if the previous turn proposed exactly these.
            details: Shown to the model.
            extra: Stored with the proposal but not compared (e.g. the name).
        """
        prev = self.state.prev_pending
        if (
            prev is not None
            and not self.state.consumed
            and prev.get("action") == info.action
            and prev.get("args") == args
        ):
            self.state.consumed = True
            return None
        self.state.new_pending = {"action": info.action, "args": args, "extra": extra or {}}
        self.state.proposal = info
        return json.dumps(
            {
                "status": "needs_confirmation",
                "details": details,
                "instruction": "Nothing has changed yet. The system now asks the customer to "
                "confirm these details with Confirm / Change buttons.",
            },
            ensure_ascii=False,
        )

    def _done(self, info: ChangeInfo) -> None:
        self.state.changed_booking = True
        self.state.done = info

    def propose_again(self, pending: dict[str, Any]) -> ToolResult:
        """Re-run a pending change in proposal mode, so the confirmation is asked again."""
        self.state.prev_pending = None
        args = {**pending.get("args", {}), **pending.get("extra", {})}
        return self.run(ToolCall("re-ask", str(pending.get("action")), args))

    def execute_pending(self, pending: dict[str, Any]) -> ToolResult:
        """Run the change the customer just confirmed with the Confirm button.

        It goes through the normal tool path, so every rule is checked again
        (the slot may have been taken since the question was asked).
        """
        self.state.prev_pending = pending
        args = {**pending.get("args", {}), **pending.get("extra", {})}
        return self.run(ToolCall("confirm-button", str(pending.get("action")), args))

    # ------------------------------------------------------------ tools

    def _tool_get_availability(self, args: dict[str, Any]) -> str:
        service = self._service(args["service_id"])
        staff_id = self._staff_id(args.get("staff_id"))
        today = self.state.now.astimezone(self._tz).date()
        day_from, day_to = parse_day(args["date_from"]), parse_day(args["date_to"])
        if day_to < day_from:
            day_from, day_to = day_to, day_from
        day_from = max(day_from, today)
        day_to = min(day_to, day_from + timedelta(days=MAX_RANGE_DAYS - 1))
        if day_to < day_from:
            return _err("in_past", "those dates have passed")
        self.state.looked_up = True
        slots = self._provider.get_availability(
            self._session, service, day_from, day_to, staff_id, self.state.now
        )
        result: dict[str, Any] = {
            "service_id": service.id,
            "range": f"{day_from.isoformat()} to {day_to.isoformat()}",
            "slots": self._slot_items(slots[:MAX_SLOTS_TO_MODEL]),
        }
        if slots:
            if len(slots) > MAX_SLOTS_TO_MODEL:
                result["note"] = f"{len(slots) - MAX_SLOTS_TO_MODEL} more free times not shown."
        else:
            # A full or closed day: look ahead in code, so the model never has to
            # guess which later day is free (§5: only offer returned slots).
            slots = self._next_free(service, day_to, staff_id)
            result["requested_range_has_free_times"] = False
            result["next_free_slots"] = self._slot_items(slots[:MAX_SLOTS_TO_MODEL])
            result["note"] = (
                "Tell the customer plainly that the requested day(s) have no free times, "
                "then offer next_free_slots."
                if slots
                else "No free times in the booking window."
            )
        if args.get("preferred_start"):
            result["preferred_start"] = self._preferred(
                service, str(args["preferred_start"]), slots
            )
        if slots:
            self.state.offered_service = service
            self.state.offered = slots
        return json.dumps(result, ensure_ascii=False)

    def _preferred(self, service: Service, text: str, slots: list[Slot]) -> dict[str, Any]:
        """Is the customer's own time free? Tells the model exactly what to do next."""
        wanted = parse_local(text, self._tz)
        if any(s.start == wanted for s in slots):
            return {
                "start": for_model(wanted, self._tz),
                "free": True,
                "next_step": "Call create_booking now with this start (don't ask the customer "
                "first: the system shows them the details with Confirm buttons).",
            }
        same_day = [
            s
            for s in slots
            if s.start.astimezone(self._tz).date() == wanted.astimezone(self._tz).date()
        ]
        nearest = sorted(same_day or slots, key=lambda s: abs((s.start - wanted).total_seconds()))[
            :3
        ]
        return {
            "start": for_model(wanted, self._tz),
            "free": False,
            "nearest_free": [
                for_model(s.start, self._tz) for s in sorted(nearest, key=lambda s: s.start)
            ],
            "next_step": "Say that time isn't free and offer the nearest free times.",
        }

    def _slot_items(self, slots: list[Slot]) -> list[dict[str, Any]]:
        return [
            {
                "start": for_model(s.start, self._tz),
                "weekday": s.start.astimezone(self._tz).strftime("%A"),
                **({"staff_id": s.staff_id} if s.staff_id else {}),
            }
            for s in slots
        ]

    def _next_free(self, service: Service, after: date, staff_id: str | None) -> list[Slot]:
        """Free slots on the first days after ``after`` that have any, within the horizon."""
        last = self.state.now.astimezone(self._tz).date() + timedelta(
            days=self._kb.rules.booking_horizon_days
        )
        start = after + timedelta(days=1)
        while start <= last:
            end = min(start + timedelta(days=MAX_RANGE_DAYS - 1), last)
            slots = self._provider.get_availability(
                self._session, service, start, end, staff_id, self.state.now
            )
            if slots:
                # The first free day plus the next one, so the customer has a choice.
                days = sorted({s.start.astimezone(self._tz).date() for s in slots})[:2]
                return [s for s in slots if s.start.astimezone(self._tz).date() in days]
            start = end + timedelta(days=1)
        return []

    def _tool_create_booking(self, args: dict[str, Any]) -> str:
        service = self._service(args["service_id"])
        staff_id = self._staff_id(args.get("staff_id"))
        start = parse_local(str(args["start"]), self._tz)
        name = str(args.get("customer_name") or "").strip()[:120]
        notes = str(args.get("notes") or "")[:500] or None
        if self.state.looked_up and not self.state.customer_chose_time:
            raise BookingError(
                "time_not_chosen",
                "The customer hasn't chosen a time. Don't choose one for them: invite them to "
                "pick from the list of free times shown under your message.",
            )
        # "Any staff" is resolved to the staff member the slot belongs to, so the
        # customer confirms (and gets) a named person.
        staff_id = self._require_offered(service, start, staff_id).staff_id
        key = {"service_id": service.id, "start": for_model(start, self._tz), "staff_id": staff_id}
        info = ChangeInfo("create_booking", service, start, staff_id)
        details = self._describe(service, start, staff_id)
        extra = {"customer_name": name, "notes": notes}
        if name:
            self._customer.name = name  # the customer just told us; keep it for the next turn
        if (wait := self._confirmed(info, key, details, extra)) is not None:
            return wait
        booking = self._provider.create(
            self._session, self._customer, service, start, staff_id, notes, self.state.now
        )
        reminders.sync_for_booking(self._session, self._settings, booking, self.state.now)
        followups.cancel_pending_for(self._session, self._customer.id)  # they booked
        self._done(ChangeInfo("create_booking", service, start, booking.staff_id))
        return _ok(booking_id=booking.id, **self._describe(service, start, booking.staff_id))

    def _tool_list_customer_bookings(self, args: dict[str, Any]) -> str:
        known = (
            self._session.execute(
                select(Booking).where(
                    Booking.customer_id == self._customer.id,
                    Booking.status == BookingStatus.CONFIRMED,
                )
            )
            .scalars()
            .all()
        )
        self.state.listed = True
        bookings = self._provider.list_for_customer(self._session, self._customer, self.state.now)
        # The adapter may have followed a staff edit in the calendar: keep reminders in step.
        for b in known:
            reminders.ensure_current(self._session, self._settings, b, self.state.now)
        items = []
        for b in bookings:
            svc = self._kb.service(b.service_id)
            # Say up front whether the rules still allow a change, so the model
            # states the notice policy instead of asking for a new time first.
            try:
                check_change_allowed(self._kb, b, self.state.now)
                can_change = True
            except BookingError:
                can_change = False
                self.state.unchangeable_listed = True
            items.append(
                {
                    "booking_id": b.id,
                    "can_change": can_change,
                    **(
                        {}
                        if can_change
                        else {
                            "why_not": f"changes need {self._kb.rules.min_cancel_notice_hours:g}"
                            "h notice"
                        }
                    ),
                    **(
                        self._describe(svc, as_utc(b.start_at), b.staff_id)
                        if svc
                        else {
                            "service_id": b.service_id,
                            "start": for_model(as_utc(b.start_at), self._tz),
                        }
                    ),
                }
            )
        return json.dumps({"bookings": items}, ensure_ascii=False)

    def _tool_reschedule_booking(self, args: dict[str, Any]) -> str:
        booking = self._own_booking(args["booking_id"])
        check_change_allowed(self._kb, booking, self.state.now)
        service = self._service(booking.service_id)
        new_start = parse_local(str(args["new_start"]), self._tz)
        self._provider.check_reschedule(self._session, booking, new_start, self.state.now)
        old_start = as_utc(booking.start_at)
        key = {"booking_id": booking.id, "new_start": for_model(new_start, self._tz)}
        details = {
            "from": for_model(old_start, self._tz),
            "to": self._describe(service, new_start, booking.staff_id),
        }
        info = ChangeInfo("reschedule_booking", service, new_start, booking.staff_id, old_start)
        if (wait := self._confirmed(info, key, details)) is not None:
            return wait
        self._provider.reschedule(self._session, booking, new_start, self.state.now)
        reminders.sync_for_booking(self._session, self._settings, booking, self.state.now)
        self._done(info)
        return _ok(booking_id=booking.id, **self._describe(service, new_start, booking.staff_id))

    def _tool_cancel_booking(self, args: dict[str, Any]) -> str:
        booking = self._own_booking(args["booking_id"])
        check_change_allowed(self._kb, booking, self.state.now)
        service = self._service(booking.service_id)
        start = as_utc(booking.start_at)
        details = {
            **self._describe(service, start, booking.staff_id),
            "cancellation_policy": self._kb.rules.cancellation_policy_en,
        }
        info = ChangeInfo("cancel_booking", service, start, booking.staff_id)
        if (wait := self._confirmed(info, {"booking_id": booking.id}, details)) is not None:
            return wait
        self._provider.cancel(self._session, booking, args.get("reason"), self.state.now)
        reminders.sync_for_booking(self._session, self._settings, booking, self.state.now)
        self._done(info)
        return _ok(booking_id=booking.id, cancelled=True)

    def _tool_mark_lead_interest(self, args: dict[str, Any]) -> str:
        service = self._service(args["service_id"])
        recorded = followups.record_interest(
            self._session, self._customer, service.id, self.state.now
        )
        return _ok(noted=True, already_noted=recorded is None)

    def _tool_handoff_to_human(self, args: dict[str, Any]) -> str:
        hand_off(self._conv, str(args.get("reason") or "other"), str(args.get("summary") or ""))
        self.state.handed_off = True
        return _ok(handed_off=True)


def hand_off(conv: Conversation, reason: str, summary: str) -> None:
    """Put a conversation in HUMAN state (§5): staff see it in the inbox, the bot goes quiet."""
    conv.state = ConversationState.HUMAN
    conv.handed_off_at = utcnow()
    conv.handoff_reason = reason[:64]
    conv.handoff_summary = summary[:1000] or None
    conv.pending_action = None
    conv.unread_for_staff = max(conv.unread_for_staff, 1)
