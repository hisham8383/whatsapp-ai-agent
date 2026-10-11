"""Opt-out detection and handling (CLAUDE.md §4), plus the other handler paths:
reminder quick replies and staff alerts on handoff (§5)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.conversation.handler import CANNED, RESCHEDULE_REQUEST, InboundHandler
from app.conversation.outbound import store_template_message
from app.conversation.replier import EchoReplier, Replier, Reply, ReplyContext
from app.conversation.tools import hand_off
from app.db.models import (
    Booking,
    Conversation,
    ConversationState,
    Customer,
    FollowUp,
    Reminder,
    SendStatus,
    StaffAlert,
)
from app.kb.loader import KnowledgeBase
from app.scheduler import followups, reminders
from app.webhook.parse import InboundMessage, ParsedWebhook
from app.webhook.service import ingest
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.optout import Classification, classify
from app.worker.queue import JOB_HANDLE_INBOUND
from app.worker.runner import Worker
from tests.conftest import NOW, FakeMeta

WA_ID = "966501234567"
_IDS = itertools.count(1)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("STOP", Classification.FOLLOWUPS),
        ("stop.", Classification.FOLLOWUPS),
        ("Unsubscribe", Classification.FOLLOWUPS),
        ("إيقاف", Classification.FOLLOWUPS),
        ("ايقاف", Classification.FOLLOWUPS),
        ("لا ترسل", Classification.FOLLOWUPS),
        ("لا ترسلون لي", Classification.FOLLOWUPS),
        ("إلغاء الاشتراك", Classification.FOLLOWUPS),
        ("don't message me", Classification.FOLLOWUPS),
        ("stop reminders", Classification.REMINDERS),
        ("إيقاف التذكير", Classification.REMINDERS),
        ("الغاء", Classification.CANCEL_WORD),
        ("إلغاء", Classification.CANCEL_WORD),
        ("cancel", Classification.CANCEL_WORD),
        ("ابي الغي موعدي", Classification.NONE),
        ("don't stop the booking", Classification.NONE),
        ("", Classification.NONE),
        (None, Classification.NONE),
    ],
)
def test_classify(text: str | None, kind: Classification) -> None:
    assert classify(text) is kind


# ------------------------------------------------------------ handler flows


class HandoffReplier:
    """Hands every message to staff, like the model calling handoff_to_human."""

    def reply(self, ctx: ReplyContext) -> Reply | None:
        hand_off(ctx.conversation, "asked_for_human", "Wants to talk to a person.")
        return Reply("handing over")


class RecordingReplier(EchoReplier):
    def __init__(self) -> None:
        self.seen: list[str | None] = []

    def reply(self, ctx: ReplyContext) -> Reply | None:
        self.seen.append(ctx.text)
        return super().reply(ctx)


def _say(
    factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    text: str,
    replier: Replier | None = None,
    reply_id: str | None = None,
) -> None:
    msg = InboundMessage(
        f"wamid.IN{next(_IDS)}",
        WA_ID,
        "Ahmed",
        "button" if reply_id else "text",
        text,
        reply_id,
        "button" if reply_id else "text",
        datetime.now(UTC),
    )
    with factory() as s:
        ingest(s, ParsedWebhook(messages=[msg], statuses=[]))
        s.commit()
    handler = InboundHandler(settings, wa_client, replier or EchoReplier(), clock=lambda: NOW)
    assert Worker({JOB_HANDLE_INBOUND: handler.handle}).run_once(factory)


def _customer(factory: sessionmaker[Session]) -> Customer:
    with factory() as s:
        return s.execute(select(Customer)).scalar_one()


def test_opt_out_confirmed_once_and_followups_cancelled(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _say(session_factory, settings, wa_client, "مرحبا")
    with session_factory() as s:
        customer = s.execute(select(Customer)).scalar_one()
        followups.record_interest(s, customer, "whitening", NOW - timedelta(days=1))
        followups.plan_due(s, settings, NOW)
        s.commit()
    _say(session_factory, settings, wa_client, "إيقاف")
    _say(session_factory, settings, wa_client, "STOP")
    assert fake_meta.texts[1:] == [CANNED["opt_out_followups"]["ar"]]  # confirmed once
    assert _customer(session_factory).opted_out_followups
    with session_factory() as s:
        assert s.execute(select(FollowUp.status)).scalar_one() is SendStatus.CANCELLED


def test_bare_cancel_goes_to_the_assistant(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
) -> None:
    replier = RecordingReplier()
    _say(session_factory, settings, wa_client, "الغاء", replier)
    assert replier.seen == ["الغاء"]  # "cancel my booking", not an opt-out
    assert not _customer(session_factory).opted_out_followups


def test_bare_cancel_after_followup_is_an_opt_out(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _say(session_factory, settings, wa_client, "hello")
    with session_factory() as s:
        customer = s.execute(select(Customer)).scalar_one()
        store_template_message(s, customer, "Hi Ahmed, you asked about whitening...", "wamid.F1")
        s.add(
            FollowUp(
                customer_id=customer.id,
                service_id="whitening",
                send_at=NOW,
                template_name="lead_followup",
                language="en",
                status=SendStatus.SENT,
                wa_message_id="wamid.F1",
            )
        )
        s.commit()
    replier = RecordingReplier()
    _say(session_factory, settings, wa_client, "cancel", replier)
    assert replier.seen == []
    assert _customer(session_factory).opted_out_followups
    assert fake_meta.texts[-1] == CANNED["opt_out_followups"]["en"]


def test_reminder_opt_out_cancels_pending_reminders(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
) -> None:
    _say(session_factory, settings, wa_client, "hi")
    with session_factory() as s:
        customer = s.execute(select(Customer)).scalar_one()
        start = datetime.now(UTC) + timedelta(days=3)
        booking = Booking(
            customer_id=customer.id,
            service_id="cleaning",
            start_at=start,
            end_at=start + timedelta(hours=1),
        )
        s.add(booking)
        s.flush()
        reminders.sync_for_booking(s, settings, booking, datetime.now(UTC))
        s.commit()
    _say(session_factory, settings, wa_client, "stop reminders")
    customer = _customer(session_factory)
    assert customer.opted_out_reminders and not customer.opted_out_followups
    with session_factory() as s:
        assert set(s.execute(select(Reminder.status)).scalars()) == {SendStatus.CANCELLED}


def _reminded(factory: sessionmaker[Session], settings: Settings) -> tuple[int, datetime]:
    with factory() as s:
        customer = s.execute(select(Customer)).scalar_one()
        start = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # Mon 10:00 Riyadh
        booking = Booking(
            customer_id=customer.id,
            service_id="cleaning",
            start_at=start,
            end_at=start + timedelta(hours=1),
        )
        s.add(booking)
        s.flush()
        [first, _] = reminders.sync_for_booking(s, settings, booking, NOW)
        s.commit()
        return first.id, start


def test_reminder_confirm_and_reschedule_taps(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _say(session_factory, settings, wa_client, "hello")
    reminder_id, _ = _reminded(session_factory, settings)
    _say(session_factory, settings, wa_client, "Confirm", reply_id=f"rem:{reminder_id}:confirm")
    assert fake_meta.texts[-1] == CANNED["reminder_confirmed"]["en"]

    # Staff had taken over meanwhile: the Reschedule tap still reaches the assistant (§5).
    with session_factory() as s:
        conv = s.execute(select(Conversation)).scalar_one()
        hand_off(conv, "other", "x")
        s.commit()
    replier = RecordingReplier()
    _say(
        session_factory,
        settings,
        wa_client,
        "Reschedule",
        replier,
        reply_id=f"rem:{reminder_id}:reschedule",
    )
    [seen] = replier.seen
    assert seen is not None
    assert seen.startswith(RESCHEDULE_REQUEST["en"].format(when="Monday 5 October at 10:00"))
    assert "[Tapped Reschedule on the reminder for booking_id " in seen
    with session_factory() as s:
        assert s.execute(select(Conversation.state)).scalar_one() is ConversationState.BOT


def test_foreign_reminder_payload_is_plain_text(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
) -> None:
    replier = RecordingReplier()
    _say(session_factory, settings, wa_client, "Confirm", replier, reply_id="rem:999:confirm")
    assert replier.seen == ["Confirm"]


def test_handoff_queues_staff_alerts(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
) -> None:
    _say(session_factory, settings, wa_client, "I want a manager", HandoffReplier())
    with session_factory() as s:
        [alert] = s.execute(select(StaffAlert)).scalars().all()
        assert (alert.contact, alert.status) == ("966500000900", SendStatus.PENDING)
    # The next customer message while HUMAN doesn't queue more alerts.
    _say(session_factory, settings, wa_client, "hello?", HandoffReplier())
    with session_factory() as s:
        assert len(s.execute(select(StaffAlert)).scalars().all()) == 1


def _say_at(
    factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    text: str,
    at: datetime,
    replier: Replier | None = None,
) -> None:
    msg = InboundMessage(f"wamid.IN{next(_IDS)}", WA_ID, "Ahmed", "text", text, None, "text", at)
    with factory() as s:
        ingest(s, ParsedWebhook(messages=[msg], statuses=[]))
        s.commit()
    handler = InboundHandler(settings, wa_client, replier or EchoReplier(), clock=lambda: NOW)
    assert Worker({JOB_HANDLE_INBOUND: handler.handle}).run_once(factory)


def test_late_redelivered_message_is_not_answered(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    """Seen live: Meta redelivered a 14-hour-old message after newer ones had arrived."""
    now = datetime.now(UTC)
    _say_at(session_factory, settings, wa_client, "hello", now)
    replier = RecordingReplier()
    _say_at(
        session_factory, settings, wa_client, "book Tuesday 9am", now - timedelta(hours=14), replier
    )
    assert replier.seen == []  # stored, not answered
    assert len(fake_meta.requests) == 1
    with session_factory() as s:
        conv = s.execute(select(Conversation)).scalar_one()
        last = conv.last_customer_msg_at
        assert last is not None
        assert (last if last.tzinfo else last.replace(tzinfo=UTC)) == now  # window not shrunk


def test_late_stop_is_still_honored_silently(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    now = datetime.now(UTC)
    _say_at(session_factory, settings, wa_client, "hello", now)
    _say_at(session_factory, settings, wa_client, "STOP", now - timedelta(hours=3))
    assert _customer(session_factory).opted_out_followups
    assert len(fake_meta.requests) == 1  # no out-of-order confirmation


def test_late_reschedule_tap_states_policy_and_hands_off(
    session_factory: sessionmaker[Session],
    settings: Settings,
    kb: KnowledgeBase,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    """Seen live: Reschedule on a 2h reminder hit the 12h change-notice rule and the
    model handed off without saying why. Now code states the policy and alerts staff."""
    _say(session_factory, settings, wa_client, "hello")
    with session_factory() as s:
        customer = s.execute(select(Customer)).scalar_one()
        start = NOW + timedelta(hours=5)  # inside the demo clinic's 12h notice
        booking = Booking(
            customer_id=customer.id,
            service_id="cleaning",
            start_at=start,
            end_at=start + timedelta(minutes=45),
        )
        s.add(booking)
        s.flush()
        [reminder] = reminders.sync_for_booking(s, settings, booking, NOW)
        s.commit()
        reminder_id = reminder.id
    replier = RecordingReplier()
    msg = InboundMessage(
        f"wamid.IN{next(_IDS)}",
        WA_ID,
        "Ahmed",
        "button",
        "Reschedule",
        f"rem:{reminder_id}:reschedule",
        "button",
        datetime.now(UTC),
    )
    with session_factory() as s:
        ingest(s, ParsedWebhook(messages=[msg], statuses=[]))
        s.commit()
    handler = InboundHandler(settings, wa_client, replier, clock=lambda: NOW, kb=kb)
    assert Worker({JOB_HANDLE_INBOUND: handler.handle}).run_once(session_factory)
    assert replier.seen == []  # no model turn
    assert "12 hours" in fake_meta.texts[-1] and "team" in fake_meta.texts[-1]
    with session_factory() as s:
        conv = s.execute(select(Conversation)).scalar_one()
        assert (conv.state, conv.handoff_reason) == (ConversationState.HUMAN, "late_change")
        assert s.execute(select(StaffAlert)).scalars().all()
