"""Webhook payload parsing."""

from __future__ import annotations

from app.webhook.parse import parse_webhook
from tests.conftest import load_fixture


def test_text() -> None:
    [msg] = parse_webhook(load_fixture("text.json")).messages
    assert msg.type == "text"
    assert msg.wa_id == "966501234567"
    assert msg.profile_name == "Ahmed"
    assert msg.text is not None and msg.text.startswith("السلام")


def test_interactive_list_reply() -> None:
    [msg] = parse_webhook(load_fixture("interactive_list_reply.json")).messages
    assert msg.type == "interactive"
    assert msg.reply_id == "slot_2026-10-01T10:00"
    assert msg.text == "Wed 10:00"


def test_image_is_unsupported() -> None:
    [msg] = parse_webhook(load_fixture("image.json")).messages
    assert msg.type == "unsupported"
    assert msg.original_type == "image"
    assert msg.text is None


def test_template_quick_reply() -> None:
    # A tap on a reminder's "Reschedule" button: the payload routes it (§4).
    [msg] = parse_webhook(load_fixture("template_quick_reply.json")).messages
    assert msg.type == "button"
    assert msg.reply_id == "rem:1:reschedule"
    assert msg.text == "تغيير الموعد"


def test_status_sent() -> None:
    [st] = parse_webhook(load_fixture("status_sent.json")).statuses
    assert (st.status, st.error_code) == ("sent", None)


def test_status() -> None:
    parsed = parse_webhook(load_fixture("status_failed.json"))
    assert parsed.messages == []
    [st] = parsed.statuses
    assert st.status == "failed"
    assert st.error_code == "131047"


def test_foreign_object_ignored() -> None:
    assert parse_webhook({"object": "page", "entry": []}).messages == []


def test_malformed_message_skipped() -> None:
    payload = load_fixture("text.json")
    del payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    assert parse_webhook(payload).messages == []
