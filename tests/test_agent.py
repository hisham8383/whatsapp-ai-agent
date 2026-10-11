"""AgentReplier with a scripted LLM: tool loop, confirmation guard, slot list, handoff, failures.

The LLM is replaced by ``ScriptedLLM``, which plays back fixed steps and records
what it was sent. The booking provider is the real ``InternalProvider`` on SQLite.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.booking.internal import InternalProvider
from app.config import Settings
from app.conversation.agent import FAILURE_TEXT, HANDOFF_TEXT, AgentReplier
from app.conversation.messages import CHANGE_ID, CONFIRM_ID
from app.conversation.replier import Reply, ReplyContext
from app.db.models import (
    Booking,
    BookingStatus,
    Conversation,
    ConversationState,
    Customer,
    Direction,
    LeadInterest,
    Message,
    MessageStatus,
    Reminder,
    Sender,
    SendStatus,
)
from app.http import ExternalServiceError
from app.kb.loader import KnowledgeBase
from app.llm.base import LLMReply, TextBlock, ToolCall, ToolResult, ToolSpec, Turn
from app.whatsapp.client import ListRow
from tests.conftest import NOW, make_conversation

_ids = itertools.count(1)
MONDAY = date(2026, 10, 5)
MONDAY_CLEANING = {"service_id": "cleaning", "date_from": "2026-10-05", "date_to": "2026-10-05"}
Step = LLMReply | Exception


def say(text: str) -> LLMReply:
    return LLMReply(blocks=(TextBlock(text),), stop_reason="end_turn", model="scripted")


def call(name: str, **args: Any) -> LLMReply:
    return LLMReply(
        blocks=(ToolCall(f"tu_{next(_ids)}", name, args),), stop_reason="tool_use", model="scripted"
    )


class ScriptedLLM:
    """Plays back ``steps`` in order and records every request."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[tuple[str, list[Turn], list[ToolSpec]]] = []

    def queue(self, *steps: Step) -> None:
        self.steps.extend(steps)

    def complete(self, system: str, history: list[Turn], tools: list[ToolSpec]) -> LLMReply:
        self.requests.append((system, list(history), list(tools)))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def tool_results(self) -> list[dict[str, Any]]:
        """Every tool result sent back to the model so far, parsed."""
        # Each request ends with the results of the step before it, so every result
        # appears exactly once as the last turn of some request.
        return [
            json.loads(block.content)
            for _, history, _ in self.requests
            for block in history[-1].blocks
            if isinstance(block, ToolResult)
        ]


@pytest.fixture
def conv_customer(session: Session) -> tuple[Conversation, Customer]:
    return make_conversation(session)


def _agent(settings: Settings, kb: KnowledgeBase, llm: ScriptedLLM, provider: Any) -> AgentReplier:
    return AgentReplier(settings, kb, llm, provider, clock=lambda: NOW)


def _turn(
    agent: AgentReplier,
    session: Session,
    conv: Conversation,
    customer: Customer,
    text: str,
    language: str = "en",
    reply_id: str | None = None,
) -> Reply | None:
    return agent.reply(
        ReplyContext(session, conv, customer, language, text, reply_id, inbound_message_id=None)
    )


def _bookings(session: Session) -> list[Booking]:
    return list(session.execute(select(Booking)).scalars())


def _user_text(turn: Turn) -> str:
    block = turn.blocks[0]
    assert isinstance(block, TextBlock)
    return block.text


# ------------------------------------------------------------------ basics


def test_faq_answer_and_request_shape(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(say("Yes, free parking in front of the clinic."))
    reply = _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "parking?")
    assert reply == Reply("Yes, free parking in front of the clinic.")
    system, history, tools = llm.requests[0]
    assert "Ibtisama Demo Dental Clinic" in system and "Q (en): Is there parking?" in system
    assert [t.name for t in tools] == [
        "get_availability",
        "create_booking",
        "list_customer_bookings",
        "reschedule_booking",
        "cancel_booking",
        "handoff_to_human",
        "mark_lead_interest",
    ]
    first_line, message = _user_text(history[-1]).split("\n", 1)
    assert first_line == (
        "[context: now=Sunday 2026-10-04 09:00 (Asia/Riyadh); "
        "customer_name=Aisha; reply_language=en]"
    )
    assert message == "parking?"


def test_system_prompt_is_identical_between_turns(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(say("a"), say("b"))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, *conv_customer, "hi")
    _turn(agent, session, *conv_customer, "مرحبا", language="ar")
    assert llm.requests[0][0] == llm.requests[1][0]  # prompt caching needs a stable prefix


def test_history_includes_staff_and_starts_with_customer(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    for sender, body in [
        (Sender.BOT, "orphan bot line"),
        (Sender.CUSTOMER, "I have a complaint"),
        (Sender.BOT, "Passing you to the team."),
        (Sender.STAFF, "Hi, this is Mona from reception."),
    ]:
        session.add(
            Message(
                conversation_id=conv.id,
                direction=Direction.INBOUND if sender is Sender.CUSTOMER else Direction.OUTBOUND,
                sender=sender,
                type="text",
                body=body,
                status=MessageStatus.SENT,
            )
        )
    session.flush()
    llm = ScriptedLLM(say("ok"))
    _turn(_agent(settings, kb, llm, provider), session, conv, customer, "thanks")
    history = llm.requests[0][1]
    assert [t.role for t in history] == ["user", "assistant", "user"]
    assert _user_text(history[0]) == "I have a complaint"
    assert _user_text(history[1]) == (
        "Passing you to the team.\n\n[Staff member wrote]: Hi, this is Mona from reception."
    )


# ------------------------------------------------------------------ confirmation guard


def test_booking_needs_confirmation_on_the_next_turn(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", **args))
    agent = _agent(settings, kb, llm, provider)
    reply = _turn(agent, session, conv, customer, "book cleaning Monday 10am")
    # The model is stopped; code writes the confirmation with buttons.
    assert len(llm.requests) == 1
    assert reply == Reply(
        "Please confirm your booking:\nTeeth cleaning with Dr. Sara\n"
        "Monday 5 October at 10:00\n45 min · 250 SAR",
        buttons=(ListRow(CONFIRM_ID, "Yes, book it"), ListRow(CHANGE_ID, "Change")),
    )
    assert _bookings(session) == []
    assert conv.pending_action == {
        "action": "create_booking",
        "args": {"service_id": "cleaning", "start": "2026-10-05 10:00", "staff_id": "dr-sara"},
        "extra": {"customer_name": "Aisha", "notes": None},
    }

    # Typed "yes": the model repeats the call, which now runs.
    llm.queue(call("create_booking", **args), say("unused: code writes the done message"))
    reply = _turn(agent, session, conv, customer, "yes please")
    [booking] = _bookings(session)
    assert booking.service_id == "cleaning" and booking.staff_id == "dr-sara"
    assert reply is not None and reply.text.startswith("You're booked ✅")
    assert conv.pending_action is None


def test_confirm_button_books_without_the_model(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    customer.name = None
    args = {"service_id": "whitening", "start": "2026-10-05 12:00", "customer_name": "Noura"}
    llm = ScriptedLLM(call("create_booking", **args))
    agent = _agent(settings, kb, llm, provider)
    reply = _turn(agent, session, conv, customer, "تبييض الاثنين 12", language="ar")
    assert reply is not None and reply.buttons[0].title == "نعم، أكّد الحجز"
    assert "يوم الاثنين 5 أكتوبر الساعة 12:00" in reply.text
    reply = _turn(agent, session, conv, customer, "نعم، أكّد الحجز", "ar", reply_id=CONFIRM_ID)
    assert len(llm.requests) == 1  # no model call for the tap
    assert reply is not None and reply.text.startswith("تم الحجز ✅")
    assert customer.name == "Noura"
    assert [b.service_id for b in _bookings(session)] == ["whitening"]


def test_confirm_button_when_the_slot_was_taken_meanwhile(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", **args))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, conv, customer, "cleaning monday 10")
    _, other = make_conversation(session, wa_id="966500000099", name="Other")
    cleaning = kb.service("cleaning")
    assert cleaning
    start = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    provider.create(session, other, cleaning, start, None, None, NOW)
    llm.queue(say("Sorry, 10:00 was just taken. 10:30 is free, would that work?"))
    reply = _turn(agent, session, conv, customer, "Yes, book it", reply_id=CONFIRM_ID)
    assert reply == Reply("Sorry, 10:00 was just taken. 10:30 is free, would that work?")
    assert "the change failed" in _user_text(llm.requests[-1][1][-1])
    assert len(_bookings(session)) == 1  # only the other customer's


def test_change_button_and_stale_confirm_go_to_the_model(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", **args), say("What would you like to change?"))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, conv, customer, "cleaning monday 10")
    _turn(agent, session, conv, customer, "Change", reply_id=CHANGE_ID)
    assert "they don't want the change" in _user_text(llm.requests[-1][1][-1])
    assert conv.pending_action is None
    llm.queue(say("What can I do for you?"))
    _turn(agent, session, conv, customer, "Yes, book it", reply_id=CONFIRM_ID)
    assert "nothing is waiting" in _user_text(llm.requests[-1][1][-1])
    assert _bookings(session) == []


def test_the_model_is_stopped_at_the_first_proposal(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(
        call("create_booking", **args), call("create_booking", **args), say("Booked!")
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "just book it")
    assert _bookings(session) == []
    assert len(llm.requests) == 1  # it never got the chance to repeat the call or say "Booked!"
    assert reply is not None and reply.buttons


def test_a_proposal_expires_after_one_customer_message(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", **args))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, conv, customer, "cleaning monday 10")
    llm.queue(say("Our hours are 9 to 9."))
    _turn(agent, session, conv, customer, "what are your hours?")
    assert conv.pending_action is None
    llm.queue(call("create_booking", **args))
    reply = _turn(agent, session, conv, customer, "ok book it")
    assert _bookings(session) == []  # the old proposal didn't carry over: asked again
    assert reply is not None and reply.buttons


def test_changed_details_need_a_new_confirmation(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    base = {"service_id": "cleaning", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", start="2026-10-05 10:00", **base))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, conv, customer, "cleaning monday 10")
    llm.queue(call("create_booking", start="2026-10-05 11:00", **base))
    _turn(agent, session, conv, customer, "yes but make it 11")
    assert _bookings(session) == []
    assert conv.pending_action and conv.pending_action["args"]["start"] == "2026-10-05 11:00"


def test_times_not_returned_by_availability_are_rejected(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(
        call("create_booking", service_id="cleaning", start="2026-10-09 10:00", customer_name="A"),
        say("Sorry, Friday is closed."),
    )
    _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "friday 10?")
    assert llm.tool_results()[0]["error"] == "slot_unavailable"
    assert conv_customer[0].pending_action is None


def test_reschedule_and_cancel_go_through_the_guard(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    cleaning = kb.service("cleaning")
    assert cleaning
    slots = provider.get_availability(session, cleaning, MONDAY, MONDAY, None, NOW)
    booking = provider.create(session, customer, cleaning, slots[0].start, None, None, NOW)
    llm = ScriptedLLM(
        call("list_customer_bookings"),
        call("reschedule_booking", booking_id=booking.id, new_start="2026-10-05 14:00"),
    )
    agent = _agent(settings, kb, llm, provider)
    reply = _turn(agent, session, conv, customer, "can I move my appointment to 2pm?")
    assert llm.tool_results()[0]["bookings"][0]["booking_id"] == booking.id
    assert reply is not None and "From: Monday 5 October at 09:00" in reply.text
    assert "To: Monday 5 October at 14:00" in reply.text
    assert booking.start_at == slots[0].start  # unchanged until confirmed

    reply = _turn(agent, session, conv, customer, "Yes, move it", reply_id=CONFIRM_ID)
    assert reply is not None and reply.text.startswith("Your appointment is moved ✅")
    assert provider.list_for_customer(session, customer, NOW)[0].start_at.hour == 11  # 14:00+3

    llm.queue(call("cancel_booking", booking_id=booking.id))
    reply = _turn(agent, session, conv, customer, "actually cancel it")
    assert reply is not None and kb.rules.cancellation_policy_en in reply.text
    assert [b.title for b in reply.buttons] == ["Yes, cancel it", "No, keep it"]
    assert booking.status is BookingStatus.CONFIRMED
    llm.queue(call("cancel_booking", booking_id=booking.id), say("unused"))
    _turn(agent, session, conv, customer, "yes")
    assert str(booking.status) == BookingStatus.CANCELLED.value


def test_cannot_touch_someone_elses_booking(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    _, other = make_conversation(session, wa_id="966500000099", name="Other")
    cleaning = kb.service("cleaning")
    assert cleaning
    slot = provider.get_availability(
        session, cleaning, date(2026, 10, 5), date(2026, 10, 5), None, NOW
    )[0]
    theirs = provider.create(session, other, cleaning, slot.start, None, None, NOW)
    llm = ScriptedLLM(call("cancel_booking", booking_id=theirs.id), say("I can't find it."))
    _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "cancel booking 1")
    assert llm.tool_results()[0]["error"] == "not_found"


# ------------------------------------------------------------------ slot list


def test_availability_becomes_an_interactive_list(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(
        call(
            "get_availability", service_id="cleaning", date_from="2026-10-05", date_to="2026-10-05"
        ),
        say("Here are Monday's free times, pick one from the list."),
    )
    reply = _turn(
        _agent(settings, kb, llm, provider), session, *conv_customer, "cleaning monday?", "ar"
    )
    assert reply is not None
    assert reply.button == "المواعيد المتاحة"
    assert len(reply.rows) == 10
    assert reply.rows[0].id == "slot|cleaning|2026-10-05T09:00|dr-sara"
    assert reply.rows[0].title == "الاثنين 5 أكتوبر 09:00"
    assert reply.rows[0].description == "د. سارة"


def test_picked_slot_is_spelled_out_for_the_model(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(say("Shall I book it?"))
    _turn(
        _agent(settings, kb, llm, provider),
        session,
        *conv_customer,
        "Mon 5 Oct, 09:00",
        reply_id="slot|cleaning|2026-10-05T09:00|dr-sara",
    )
    assert _user_text(llm.requests[0][1][-1]).endswith(
        "Mon 5 Oct, 09:00\n[Picked from the list: service_id=cleaning, "
        "start=2026-10-05 09:00, staff_id=dr-sara]"
    )


# ------------------------------------------------------------------ handoff and failures


def test_handoff_sets_human_state_and_uses_fixed_wording(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    conv.pending_action = {"action": "cancel_booking", "args": {"booking_id": 1}}
    llm = ScriptedLLM(
        call("handoff_to_human", reason="complaint", summary="Unhappy with her last visit.")
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "I'm very upset")
    assert len(llm.requests) == 1  # nothing more is asked of the model after handing off
    assert reply == Reply(f"{HANDOFF_TEXT['en']} Our team replies Sunday-Thursday, 9am-9pm.")
    assert conv.state is ConversationState.HUMAN
    assert conv.handoff_reason == "complaint"
    assert conv.handoff_summary == "Unhappy with her last visit."
    assert conv.unread_for_staff == 1 and conv.pending_action is None


def test_bilingual_hours_message(settings: Settings) -> None:
    both = settings.model_copy(
        update={"handoff_hours_message": "نرد من ٩ إلى ٩ || We reply 9 to 9"}
    )
    assert both.handoff_hours_for("ar") == "نرد من ٩ إلى ٩"
    assert both.handoff_hours_for("en") == "We reply 9 to 9"
    assert settings.handoff_hours_for("ar") == settings.handoff_hours_message


def test_two_failures_in_a_row_hand_off(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    boom = ExternalServiceError("anthropic", "HTTP 529", 529)
    llm = ScriptedLLM(boom)
    agent = _agent(settings, kb, llm, provider)
    assert _turn(agent, session, conv, customer, "hello?") == Reply(FAILURE_TEXT["en"])
    assert conv.state is ConversationState.BOT and conv.bot_failures == 1
    llm.queue(boom)
    reply = _turn(agent, session, conv, customer, "hello??")
    assert reply is not None and reply.text.startswith(HANDOFF_TEXT["en"])
    assert str(conv.state) == "HUMAN" and conv.handoff_reason == "repeated_failure"


def test_success_resets_the_failure_count(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    llm = ScriptedLLM(
        ExternalServiceError("anthropic", "timeout"),
        say("hi"),
        ExternalServiceError("anthropic", "timeout"),
    )
    agent = _agent(settings, kb, llm, provider)
    for text in ("a", "b", "c"):
        _turn(agent, session, conv, customer, text)
    assert conv.state is ConversationState.BOT and conv.bot_failures == 1


def test_endless_tool_loop_counts_as_a_failure(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(*[call("list_customer_bookings") for _ in range(6)])
    reply = _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "hi")
    assert reply == Reply(FAILURE_TEXT["en"])


class DownProvider(InternalProvider):
    """A booking system that is unreachable."""

    def get_availability(self, *args: Any, **kwargs: Any) -> Any:
        raise ExternalServiceError("booking", "timeout")


def test_booking_system_down_is_reported_to_the_model(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(
        call(
            "get_availability", service_id="cleaning", date_from="2026-10-05", date_to="2026-10-05"
        ),
        say("Our booking system is down; shall I connect you with the team?"),
    )
    reply = _turn(
        _agent(settings, kb, llm, DownProvider(kb, settings.tz)), session, *conv_customer, "book"
    )
    assert llm.tool_results()[0]["error"] == "booking_system_unavailable"
    assert reply is not None and not reply.rows


def test_bad_tool_arguments_become_error_results(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(
        call(
            "get_availability", service_id="massage", date_from="2026-10-05", date_to="2026-10-05"
        ),
        call("get_availability", service_id="cleaning", date_from="next monday", date_to="x"),
        call("no_such_tool"),
        say("Which service would you like?"),
    )
    _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "book")
    assert [r["error"] for r in llm.tool_results()] == [
        "unknown_service",
        "bad_arguments",
        "unknown_tool",
    ]


def test_false_done_claim_is_replaced_by_the_confirmation(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", **args))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, conv, customer, "cleaning monday 10")
    # The model says it's done without calling the tool again.
    llm.queue(say("Great, your appointment is booked!"))
    reply = _turn(agent, session, conv, customer, "sure")
    note = _user_text(llm.requests[-1][1][-1])
    assert "was just asked to confirm this change: create_booking" in note
    assert '"start": "2026-10-05 10:00"' in note and '"customer_name": "Aisha"' in note
    assert reply is not None and reply.buttons  # asked again, not a false "booked"
    assert _bookings(session) == []
    assert conv.pending_action is not None


def test_full_day_returns_the_next_free_days(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(
        call(
            "get_availability", service_id="cleaning", date_from="2026-10-09", date_to="2026-10-09"
        ),
        say("Friday is closed. Saturday afternoon or Sunday?"),
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "friday?")
    result = llm.tool_results()[0]
    assert result["slots"] == [] and result["requested_range_has_free_times"] is False
    days = {s["start"][:10] for s in result["next_free_slots"]}
    assert days == {"2026-10-10", "2026-10-11"}  # Saturday, then Sunday
    assert reply is not None and reply.rows[0].id.startswith("slot|cleaning|2026-10-10T16:00")


def test_model_cannot_choose_the_time_for_the_customer(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    llm = ScriptedLLM(
        call("get_availability", **MONDAY_CLEANING),
        call("create_booking", service_id="cleaning", start="2026-10-05 09:00", customer_name="A"),
        say("Pick a time from the list."),
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, *conv_customer, "cleaning monday")
    assert llm.tool_results()[1]["error"] == "time_not_chosen"
    assert reply is not None and reply.rows and not reply.buttons


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("tomorrow at 10am", True),
        ("الساعة ٥ العصر", True),
        ("الساعة خمس العصر", True),
        ("around noon", True),
        ("Monday please", False),
        ("أبي موعد يوم الثلاثاء", False),
    ],
)
def test_names_a_time(text: str, named: bool) -> None:
    from app.conversation.tools import names_a_time

    assert names_a_time(text) is named


# ------------------------------------------------------------------ reminders + leads


def _reminder_times(session: Session) -> list[datetime]:
    rows = session.execute(
        select(Reminder.send_at)
        .where(Reminder.status == SendStatus.PENDING)
        .order_by(Reminder.send_at)
    ).scalars()
    return [r if r.tzinfo else r.replace(tzinfo=UTC) for r in rows]


def test_booking_changes_replan_reminders(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    """Create, move and cancel through the tools; reminders follow each change (§5)."""
    conv, customer = conv_customer
    args = {"service_id": "cleaning", "start": "2026-10-05 10:00", "customer_name": "Aisha"}
    llm = ScriptedLLM(call("create_booking", **args))
    agent = _agent(settings, kb, llm, provider)
    _turn(agent, session, conv, customer, "cleaning Monday 10")
    _turn(agent, session, conv, customer, "yes", reply_id=CONFIRM_ID)
    start = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    assert _reminder_times(session) == [start - timedelta(hours=24), start - timedelta(hours=2)]

    [booking] = _bookings(session)
    move = {"booking_id": booking.id, "new_start": "2026-10-06 10:00"}
    llm.queue(call("reschedule_booking", **move))
    _turn(agent, session, conv, customer, "move it to Tuesday 10")
    _turn(agent, session, conv, customer, "yes", reply_id=CONFIRM_ID)
    moved = start + timedelta(days=1)
    assert _reminder_times(session) == [moved - timedelta(hours=24), moved - timedelta(hours=2)]

    llm.queue(call("cancel_booking", booking_id=booking.id))
    _turn(agent, session, conv, customer, "cancel it")
    _turn(agent, session, conv, customer, "yes", reply_id=CONFIRM_ID)
    assert _reminder_times(session) == []


def test_mark_lead_interest_is_silent_and_recorded(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    llm = ScriptedLLM(
        call("mark_lead_interest", service_id="whitening"),
        say("Teeth whitening is 1200 SAR."),
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "whitening price?")
    assert reply == Reply("Teeth whitening is 1200 SAR.")
    [interest] = session.execute(select(LeadInterest)).scalars().all()
    assert (interest.customer_id, interest.service_id) == (customer.id, "whitening")
    assert "mark_lead_interest" in llm.requests[0][0]  # the prompt explains when to use it


def test_prompt_has_dialect_rules_and_style_guide(settings: Settings, kb: KnowledgeBase) -> None:
    from app.conversation.prompt import build_system_prompt

    prompt = build_system_prompt(settings, kb)
    assert "natural Gulf" in prompt and "حياك" in prompt
    assert "# Arabic style guide" in prompt and "عيادة ابتسامة" in prompt
    assert prompt == build_system_prompt(settings, kb)  # byte-stable for caching
    msa = build_system_prompt(settings.model_copy(update={"dialect": "msa"}), kb)
    assert "Modern Standard Arabic (فصحى مبسطة)" in msa and "natural Gulf" not in msa


def test_answer_next_to_silent_tool_is_final(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    """Reply + mark_lead_interest in one step: send the reply, no second model call."""
    conv, customer = conv_customer
    both = LLMReply(
        blocks=(
            TextBlock("Whitening is 1200 SAR."),
            ToolCall("tu_x", "mark_lead_interest", {"service_id": "whitening"}),
        ),
        stop_reason="tool_use",
        model="scripted",
    )
    llm = ScriptedLLM(both)
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "whitening?")
    assert reply == Reply("Whitening is 1200 SAR.")
    assert len(llm.requests) == 1
    assert session.execute(select(LeadInterest)).scalar_one().service_id == "whitening"


def test_empty_turn_after_answer_uses_the_answer(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    both = LLMReply(
        blocks=(
            TextBlock("Let me check."),
            ToolCall("tu_y", "list_customer_bookings", {}),
        ),
        stop_reason="tool_use",
        model="scripted",
    )
    llm = ScriptedLLM(both, ExternalServiceError("anthropic", "empty reply (end_turn)"))
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "my bookings?")
    assert reply == Reply("Let me check.")
    assert conv.bot_failures == 0


def test_false_booked_claim_after_slot_pick_is_corrected(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    """Seen in the eval set: after a slot pick the model said "booked" without a tool call."""
    from app.conversation.agent import FALSE_CLAIM_NOTE

    conv, customer = conv_customer
    args = {"service_id": "checkup", "start": "2026-10-06 09:30", "customer_name": "Aisha"}
    llm = ScriptedLLM(say("أبشر، حجزت لك موعدك!"), call("create_booking", **args))
    reply = _turn(
        _agent(settings, kb, llm, provider),
        session,
        conv,
        customer,
        "الثلاثاء 6 أكتوبر 09:30",
        language="ar",
        reply_id="slot|checkup|2026-10-06T09:30|dr-sara",
    )
    assert reply is not None and reply.buttons  # the real confirmation, with buttons
    assert _user_text(llm.requests[1][1][-1]) == FALSE_CLAIM_NOTE
    assert _bookings(session) == []


def test_persistent_false_claim_is_never_sent(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    llm = ScriptedLLM(say("You're booked!"), say("Done, you're booked!"))
    reply = _turn(
        _agent(settings, kb, llm, provider),
        session,
        conv,
        customer,
        "Mon 5 Oct, 10:00",
        reply_id="slot|checkup|2026-10-05T10:00|dr-sara",
    )
    assert reply == Reply(FAILURE_TEXT["en"])


def test_asking_in_text_after_lookup_is_corrected(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    """Seen in the eval set: "Shall I cancel it?" in words, so the customer confirmed twice."""
    from app.conversation.agent import ASKED_IN_TEXT_NOTE

    conv, customer = conv_customer
    start = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
    booking = Booking(
        customer_id=customer.id,
        service_id="checkup",
        staff_id="dr-sara",
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(booking)
    session.flush()
    llm = ScriptedLLM(
        call("list_customer_bookings"),
        say("I found your check-up on Thursday. Shall I go ahead and cancel it?"),
        call("cancel_booking", booking_id=booking.id),
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "cancel my visit")
    assert reply is not None and reply.text.startswith("Please confirm the cancellation")
    assert _user_text(llm.requests[2][1][-1]) == ASKED_IN_TEXT_NOTE


def test_status_question_is_not_treated_as_a_false_claim(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    conv, customer = conv_customer
    llm = ScriptedLLM(call("list_customer_bookings"), say("Yes, your booking is confirmed."))
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "am I booked?")
    assert reply == Reply("Yes, your booking is confirmed.")
    assert len(llm.requests) == 2


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("You're booked for Monday!", True),
        ("I've cancelled it.", True),
        ("أبشر، حجزت لك موعدك!", True),
        ("تم حجز موعدك", True),
        ("Monday is fully booked, but Tuesday has times.", False),
        ("Sorry, that day is all booked up.", False),
        ("يوم الاثنين محجوز بالكامل", False),
    ],
)
def test_change_claim_detection(text: str, claim: bool) -> None:
    from app.conversation.agent import _CLAIMS_CHANGE

    assert bool(_CLAIMS_CHANGE.search(text)) is claim


def test_handoff_after_too_late_explains_the_policy(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    """Seen live: the model hit too_late_to_change and handed off without saying why."""
    conv, customer = conv_customer
    start = NOW + timedelta(hours=5)  # inside the demo clinic's 12h notice
    booking = Booking(
        customer_id=customer.id,
        service_id="checkup",
        staff_id="dr-sara",
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(booking)
    session.flush()
    llm = ScriptedLLM(
        call("reschedule_booking", booking_id=booking.id, new_start="2026-10-05 10:00"),
        call("handoff_to_human", reason="late_change", summary="wants to move today's visit"),
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "move it")
    assert reply is not None and reply.text.startswith("Under our policy")
    assert "12 hours" in reply.text and HANDOFF_TEXT["en"] in reply.text


def test_list_marks_unchangeable_and_handoff_states_policy(
    session: Session,
    settings: Settings,
    kb: KnowledgeBase,
    provider: InternalProvider,
    conv_customer: tuple[Conversation, Customer],
) -> None:
    """Seen in the eval set: the model listed a same-day booking and handed off silently."""
    conv, customer = conv_customer
    start = NOW + timedelta(hours=5)
    session.add(
        Booking(
            customer_id=customer.id,
            service_id="checkup",
            staff_id="dr-sara",
            start_at=start,
            end_at=start + timedelta(minutes=30),
        )
    )
    session.flush()
    llm = ScriptedLLM(
        call("list_customer_bookings"),
        call("handoff_to_human", reason="late_change", summary="wants to move today's visit"),
    )
    reply = _turn(_agent(settings, kb, llm, provider), session, conv, customer, "move my visit")
    [listing] = llm.tool_results()[:1]
    assert listing["bookings"][0]["can_change"] is False
    assert listing["bookings"][0]["why_not"] == "changes need 12h notice"
    assert reply is not None and reply.text.startswith("Under our policy")


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("تبي تحوليه للأربعاء الساعة 10 الصبح؟", True),  # seen in the eval set
        ("تبي ننقلها لبكرة؟", True),
        ("Shall I go ahead and cancel it?", True),
        ("تبي أحولك على الفريق؟", False),  # the handoff offer, not a booking change
        ("وش الموعد الجديد اللي تبيه؟", False),
    ],
)
def test_asks_to_change_detection(text: str, asks: bool) -> None:
    from app.conversation.agent import _ASKS_TO_CHANGE

    assert bool(_ASKS_TO_CHANGE.search(text)) is asks
