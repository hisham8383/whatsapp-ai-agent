"""Webhook endpoint: handshake, signature, dedupe, status updates."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Conversation,
    ConversationState,
    Direction,
    Job,
    Message,
    MessageStatus,
    ProcessedEvent,
    Sender,
)
from tests.conftest import load_fixture, post_webhook


def _count(factory: sessionmaker[Session], model: type[Any]) -> int:
    with factory() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


# ----- GET handshake --------------------------------------------------------


def test_handshake_echoes_challenge(client: TestClient) -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "test-verify-token",
        "hub.challenge": "42",
    }
    r = client.get("/webhook", params=params)
    assert r.status_code == 200
    assert r.text == "42"


def test_handshake_rejects_wrong_token(client: TestClient) -> None:
    params = {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"}
    assert client.get("/webhook", params=params).status_code == 403


# ----- signature ------------------------------------------------------------


def test_bad_signature_rejected_and_nothing_stored(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    fixture = load_fixture("bad_signature.json")
    r = client.post(
        "/webhook",
        content=json.dumps(fixture["payload"]).encode(),
        headers={"X-Hub-Signature-256": fixture["signature"]},
    )
    assert r.status_code == 401
    assert _count(session_factory, Message) == 0
    assert _count(session_factory, ProcessedEvent) == 0


def test_wrong_secret_rejected(client: TestClient) -> None:
    assert post_webhook(client, load_fixture("text.json"), secret="wrong") == 401


def test_missing_signature_rejected(client: TestClient) -> None:
    assert client.post("/webhook", content=b"{}").status_code == 401


def test_signed_invalid_json_is_400(client: TestClient) -> None:
    from tests.conftest import sign

    body = b"not json"
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert r.status_code == 400


# ----- ingest + dedupe ------------------------------------------------------


def test_text_message_stored_and_enqueued(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert post_webhook(client, load_fixture("text.json")) == 200
    with session_factory() as s:
        msg = s.execute(select(Message)).scalar_one()
        assert msg.direction is Direction.INBOUND
        assert msg.sender is Sender.CUSTOMER
        assert msg.body and "سعر" in msg.body
        conv = s.get(Conversation, msg.conversation_id)
        assert conv is not None and conv.last_customer_msg_at is not None
        job = s.execute(select(Job)).scalar_one()
        assert job.payload["wa_message_id"] == "wamid.TEXT0001"
        assert "body" not in job.payload  # message text stays in `messages` only (PDPL retention)


def test_redelivery_processed_once(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    payload = load_fixture("text.json")
    assert post_webhook(client, payload) == 200
    assert post_webhook(client, payload) == 200  # Meta retry: still 200, so it stops retrying
    assert _count(session_factory, Message) == 1
    assert _count(session_factory, Job) == 1


def test_duplicate_within_one_delivery_stored_once(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert post_webhook(client, load_fixture("duplicate.json")) == 200
    assert _count(session_factory, Message) == 1
    assert _count(session_factory, Job) == 1


def test_human_conversation_counts_unread(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    post_webhook(client, load_fixture("text.json"))
    with session_factory() as s:
        conv = s.execute(select(Conversation)).scalar_one()
        conv.state = ConversationState.HUMAN
        s.commit()
    post_webhook(client, load_fixture("image.json"))
    with session_factory() as s:
        assert s.execute(select(Conversation.unread_for_staff)).scalar_one() == 1


def test_status_update_applied(client: TestClient, session_factory: sessionmaker[Session]) -> None:
    post_webhook(client, load_fixture("text.json"))
    with session_factory() as s:
        conv_id = s.execute(select(Conversation.id)).scalar_one()
        s.add(
            Message(
                conversation_id=conv_id,
                direction=Direction.OUTBOUND,
                sender=Sender.BOT,
                wa_message_id="wamid.OUT0001",
                type="text",
                body="hi",
                status=MessageStatus.SENT,
            )
        )
        s.commit()
    assert post_webhook(client, load_fixture("status_failed.json")) == 200
    with session_factory() as s:
        out = s.execute(
            select(Message).where(Message.wa_message_id == "wamid.OUT0001")
        ).scalar_one()
        assert out.status is MessageStatus.FAILED
        assert out.error_code == "131047"


def test_status_never_moves_backwards(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    post_webhook(client, load_fixture("text.json"))
    with session_factory() as s:
        conv_id = s.execute(select(Conversation.id)).scalar_one()
        s.add(
            Message(
                conversation_id=conv_id,
                direction=Direction.OUTBOUND,
                sender=Sender.BOT,
                wa_message_id="wamid.OUT0001",
                type="text",
                body="hi",
                status=MessageStatus.READ,
            )
        )
        s.commit()
    late = load_fixture("status_failed.json")
    st = late["entry"][0]["changes"][0]["value"]["statuses"][0]
    st["status"] = "delivered"
    del st["errors"]
    post_webhook(client, late)
    with session_factory() as s:
        assert (
            s.execute(
                select(Message.status).where(Message.wa_message_id == "wamid.OUT0001")
            ).scalar_one()
            is MessageStatus.READ
        )


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
