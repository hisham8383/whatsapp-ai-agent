"""Inbound handler: echo slice, HUMAN silence, auto-release, unsupported media, window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.conversation.handler import CANNED, InboundHandler
from app.conversation.replier import EchoReplier
from app.db.models import (
    Conversation,
    ConversationState,
    Customer,
    Direction,
    Job,
    JobStatus,
    Message,
    Sender,
)
from app.webhook.parse import parse_webhook
from app.webhook.service import ingest
from app.whatsapp.client import WhatsAppClient
from app.worker.queue import JOB_HANDLE_INBOUND
from app.worker.runner import Worker
from tests.conftest import FakeMeta, freshen, load_fixture, post_webhook


def _worker(settings: Settings, wa_client: WhatsAppClient) -> Worker:
    handler = InboundHandler(settings, wa_client, EchoReplier())
    return Worker({JOB_HANDLE_INBOUND: handler.handle})


def _ingest(factory: sessionmaker[Session], fixture: str, fresh: bool = True) -> None:
    payload = load_fixture(fixture)
    with factory() as s:
        ingest(s, parse_webhook(freshen(payload) if fresh else payload))
        s.commit()


def _set_human(factory: sessionmaker[Session], handed_off_at: datetime) -> None:
    with factory() as s:
        conv = s.execute(select(Conversation)).scalar_one()
        conv.state = ConversationState.HUMAN
        conv.handed_off_at = handed_off_at
        conv.handoff_summary = "wants a refund"
        s.commit()


def test_end_to_end_signed_webhook_to_echo(
    client: TestClient,
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    """The days 1–3 'done' check: signed POST → stored → worker → echo sent through Meta."""
    assert post_webhook(client, freshen(load_fixture("text.json"))) == 200
    assert _worker(settings, wa_client).run_once(session_factory)
    assert len(fake_meta.texts) == 1 and fake_meta.texts[0].startswith("وصلتني رسالتك")
    assert fake_meta.requests[0]["to"] == "966501234567"
    with session_factory() as s:
        assert s.execute(select(Job.status)).scalar_one() is JobStatus.DONE
        out = s.execute(select(Message).where(Message.direction == Direction.OUTBOUND)).scalar_one()
        assert out.wa_message_id == "wamid.OUT1"
        assert out.sender is Sender.BOT
        assert s.execute(select(Customer.language)).scalar_one() == "ar"


def test_english_echo(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "duplicate.json")
    _worker(settings, wa_client).run_once(session_factory)
    assert fake_meta.texts == ["You said: Hi, are you open on Friday?"]


def test_interactive_reply_echoes_title(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "interactive_list_reply.json")
    _worker(settings, wa_client).run_once(session_factory)
    assert fake_meta.texts == ["You said: Wed 10:00"]


def test_human_state_stays_silent(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "text.json")
    _set_human(session_factory, datetime.now(UTC))
    _worker(settings, wa_client).run_once(session_factory)
    assert fake_meta.requests == []


def test_human_state_auto_released(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "text.json")
    hours = settings.handoff_auto_release_hours + 1
    _set_human(session_factory, datetime.now(UTC) - timedelta(hours=hours))
    _worker(settings, wa_client).run_once(session_factory)
    assert len(fake_meta.texts) == 1
    with session_factory() as s:
        conv = s.execute(select(Conversation)).scalar_one()
        assert conv.state is ConversationState.BOT
        assert conv.handoff_summary is None


def test_image_gets_text_request(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "image.json")
    _worker(settings, wa_client).run_once(session_factory)
    # No text means no language signal, so it falls back to DEFAULT_LANGUAGE (ar).
    assert fake_meta.texts == [CANNED["unsupported_media"]["ar"]]


def test_image_uses_customers_known_language(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "text.json")
    with session_factory() as s:
        s.execute(select(Customer)).scalar_one().language = "en"
        s.commit()
    worker = _worker(settings, wa_client)
    worker.run_once(session_factory)  # Arabic text: language flips back to ar
    _ingest(session_factory, "image.json")
    worker.run_once(session_factory)
    assert fake_meta.texts[-1] == CANNED["unsupported_media"]["ar"]


def test_window_closed_no_send_job_done(
    settings: Settings,
    session_factory: sessionmaker[Session],
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    _ingest(session_factory, "text.json", fresh=False)  # the fixture timestamp is > 24h ago
    _worker(settings, wa_client).run_once(session_factory)
    assert fake_meta.requests == []
    with session_factory() as s:
        # Done, not retried: retrying can't reopen the window.
        assert s.execute(select(Job.status)).scalar_one() is JobStatus.DONE
