"""WhatsApp send client: window guard on every send, request shapes, retries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.http import ExternalServiceError
from app.whatsapp.client import ListRow, WhatsAppClient
from app.whatsapp.window import OutsideServiceWindowError
from tests.conftest import FakeMeta

TO = "966501234567"
STALE = datetime.now(UTC) - timedelta(days=2)


def test_send_text_inside_window(wa_client: WhatsAppClient, fake_meta: FakeMeta) -> None:
    wamid = wa_client.send_text(TO, "hi", last_customer_msg_at=datetime.now(UTC))
    assert wamid == "wamid.OUT1"
    assert fake_meta.requests[0]["to"] == TO
    assert fake_meta.texts == ["hi"]


def test_send_text_refused_outside_window(wa_client: WhatsAppClient, fake_meta: FakeMeta) -> None:
    with pytest.raises(OutsideServiceWindowError):
        wa_client.send_text(TO, "hi", last_customer_msg_at=STALE)
    assert fake_meta.requests == []  # never reached Meta


def test_send_text_refused_if_customer_never_wrote(
    wa_client: WhatsAppClient, fake_meta: FakeMeta
) -> None:
    with pytest.raises(OutsideServiceWindowError):
        wa_client.send_text(TO, "hi", last_customer_msg_at=None)
    assert fake_meta.requests == []


def test_send_list_refused_outside_window(wa_client: WhatsAppClient, fake_meta: FakeMeta) -> None:
    with pytest.raises(OutsideServiceWindowError):
        wa_client.send_list(TO, "Pick", "Times", [ListRow("a", "A")], last_customer_msg_at=STALE)
    assert fake_meta.requests == []


def test_template_allowed_outside_window(wa_client: WhatsAppClient, fake_meta: FakeMeta) -> None:
    # §4: outside the window only templates may go out; they need no window check.
    wamid = wa_client.send_template(
        TO,
        "appointment_reminder",
        "ar",
        ["عيادة", "الاثنين 5 أكتوبر", "10:00"],
        quick_reply_payloads=["rem:1:confirm", "rem:1:reschedule"],
    )
    assert wamid == "wamid.OUT1"
    body = fake_meta.requests[0]
    assert body["type"] == "template"
    template = body["template"]
    assert template["name"] == "appointment_reminder"
    assert template["language"] == {"code": "ar"}
    params = template["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["عيادة", "الاثنين 5 أكتوبر", "10:00"]
    buttons = template["components"][1:]
    assert [(b["index"], b["parameters"][0]["payload"]) for b in buttons] == [
        ("0", "rem:1:confirm"),
        ("1", "rem:1:reschedule"),
    ]


def test_template_empty_parameter_replaced(wa_client: WhatsAppClient, fake_meta: FakeMeta) -> None:
    wa_client.send_template(TO, "staff_handoff_alert", "en", ["", "complaint"])
    params = fake_meta.requests[0]["template"]["components"][0]["parameters"]
    assert params[0]["text"] == "-"  # Meta rejects empty parameters


def test_template_timeout_not_retried(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    # A read timeout means Meta may have sent it: retrying could double-send (§2).
    monkeypatch.setattr("app.http.time.sleep", lambda _s: None)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    wa = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ExternalServiceError) as exc:
        wa.send_template(TO, "lead_followup", "en", ["a", "b", "c"])
    assert exc.value.ambiguous
    assert len(calls) == 1


def test_template_connect_error_retried(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The request never left, so a retry is safe.
    monkeypatch.setattr("app.http.time.sleep", lambda _s: None)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.Y"}]})

    wa = WhatsAppClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert wa.send_template(TO, "lead_followup", "en", ["a", "b", "c"]) == "wamid.Y"
    assert len(calls) == 2


def test_send_list_trims_to_meta_limits(wa_client: WhatsAppClient, fake_meta: FakeMeta) -> None:
    rows = [
        ListRow(f"slot_{i}", f"Wednesday 1 Oct {i:02d}:00 (long title)", "x" * 100)
        for i in range(12)
    ]
    wa_client.send_list(
        TO, "Pick a time", "Available times list", rows, last_customer_msg_at=datetime.now(UTC)
    )
    interactive = fake_meta.requests[0]["interactive"]
    sent_rows = interactive["action"]["sections"][0]["rows"]
    assert interactive["type"] == "list"
    assert len(sent_rows) == 10
    assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in sent_rows)
    assert len(interactive["action"]["button"]) <= 20


def _client(settings: Settings, responses: list[httpx.Response]) -> WhatsAppClient:
    it = iter(responses)
    transport = httpx.MockTransport(lambda _req: next(it))
    return WhatsAppClient(settings, http=httpx.Client(transport=transport))


def test_transient_error_retried(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.http.time.sleep", lambda _s: None)
    wa = _client(
        settings,
        [httpx.Response(503), httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})],
    )
    assert wa.send_text(TO, "hi", last_customer_msg_at=datetime.now(UTC)) == "wamid.X"


def test_client_error_not_retried(settings: Settings) -> None:
    wa = _client(settings, [httpx.Response(400, json={"error": {"code": 131047}})])
    with pytest.raises(ExternalServiceError) as exc:
        wa.send_text(TO, "hi", last_customer_msg_at=datetime.now(UTC))
    assert exc.value.status_code == 400


def test_unexpected_response_shape(settings: Settings) -> None:
    wa = _client(settings, [httpx.Response(200, json={"ok": True})])
    with pytest.raises(ExternalServiceError, match="unexpected"):
        wa.send_text(TO, "hi", last_customer_msg_at=datetime.now(UTC))
