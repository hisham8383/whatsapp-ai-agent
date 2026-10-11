"""Staff inbox: login, list, thread, replies within the 24h window, Return to bot (CLAUDE.md §5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.conversation.tools import hand_off
from app.db.models import Conversation, ConversationState, Message, Sender
from app.inbox.auth import (
    COOKIE_NAME,
    LoginThrottle,
    make_session_token,
    valid_session_token,
)
from app.inbox.service import WINDOW_CLOSED_TEXT
from tests.conftest import FakeMeta, make_conversation

PASSWORD = "test-inbox-secret"


def _handed_off(factory: sessionmaker[Session], window_open: bool = True) -> int:
    with factory() as s:
        conv, _ = make_conversation(s, name="Layla")
        if not window_open:
            conv.last_customer_msg_at = datetime.now(UTC) - timedelta(hours=25)
        hand_off(conv, "complaint", "Wants a refund for her last cleaning.")
        s.commit()
        return conv.id


def _login(client: TestClient) -> None:
    r = client.post(
        "/inbox/login",
        content=f"password={PASSWORD}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def _form(client: TestClient, url: str, body: str) -> tuple[int, str]:
    r = client.post(
        url,
        content=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    return int(r.status_code), str(r.headers.get("location", ""))


def test_tokens() -> None:
    token = make_session_token("s", now=1000)
    assert valid_session_token("s", token, now=1001)
    assert not valid_session_token("other", token, now=1001)
    assert not valid_session_token("s", token, now=1000 + 13 * 3600)
    assert not valid_session_token("s", "garbage", now=1001)


def test_throttle() -> None:
    t = LoginThrottle()
    for _ in range(5):
        t.failed("1.2.3.4", now=100)
    assert t.blocked("1.2.3.4", now=101)
    assert not t.blocked("1.2.3.4", now=100 + 16 * 60)


def test_pages_need_login(client: TestClient) -> None:
    for url in ("/inbox", "/inbox/partials/list", "/inbox/c/1"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/inbox/login"
    assert client.post("/inbox/c/1/reply", follow_redirects=False).status_code == 303


def test_wrong_password_then_lockout(client: TestClient) -> None:
    body = "password=nope"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    for _ in range(5):
        assert client.post("/inbox/login", content=body, headers=headers).status_code == 401
    r = client.post("/inbox/login", content=f"password={PASSWORD}", headers=headers)
    assert r.status_code == 429


def test_login_cookie_flags(client: TestClient) -> None:
    r = client.post(
        "/inbox/login",
        content=f"password={PASSWORD}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/inbox" in cookie


def test_list_shows_handoffs_and_unread_count(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _handed_off(session_factory)
    _login(client)
    page = client.get("/inbox").text
    assert "<title>(1) Staff inbox</title>" in page
    assert "Layla" in page and "Wants a refund for her last cleaning." in page
    partial = client.get("/inbox/partials/list")
    assert partial.headers["X-Unread"] == "1"


def test_thread_reply_and_return_to_bot(
    client: TestClient, session_factory: sessionmaker[Session], fake_meta: FakeMeta
) -> None:
    conv_id = _handed_off(session_factory)
    _login(client)
    page = client.get(f"/inbox/c/{conv_id}").text
    assert (
        "Return to bot" in page
        and "disabled" not in page.split('id="reply"')[1].split("</form>")[0]
    )

    arabic = "text=%D8%A3%D9%87%D9%84%D9%8B%D8%A7+Layla"
    status, location = _form(client, f"/inbox/c/{conv_id}/reply", arabic)
    assert (status, location) == (303, f"/inbox/c/{conv_id}")
    assert fake_meta.texts == ["أهلًا Layla"]
    with session_factory() as s:
        staff = s.execute(select(Message).where(Message.sender == Sender.STAFF)).scalar_one()
        assert staff.body == "أهلًا Layla" and staff.wa_message_id == "wamid.OUT1"
        assert s.get(Conversation, conv_id).unread_for_staff == 0  # type: ignore[union-attr]

    # A double click resubmits the same text: the customer gets it once.
    assert _form(client, f"/inbox/c/{conv_id}/reply", arabic)[0] == 303
    assert fake_meta.texts == ["أهلًا Layla"]
    # After a staff reply, the page reminds staff to hand the chat back.
    assert "stays silent in this chat until you click" in client.get(f"/inbox/c/{conv_id}").text
    # A different message still goes out.
    _form(client, f"/inbox/c/{conv_id}/reply", "text=See+you+Thursday")
    assert fake_meta.texts == ["أهلًا Layla", "See you Thursday"]

    assert _form(client, f"/inbox/c/{conv_id}/release", "")[0] == 303
    with session_factory() as s:
        conv = s.get(Conversation, conv_id)
        assert conv is not None
        assert conv.state is ConversationState.BOT and conv.handoff_summary is None


def test_closed_window_disables_reply(
    client: TestClient, session_factory: sessionmaker[Session], fake_meta: FakeMeta
) -> None:
    conv_id = _handed_off(session_factory, window_open=False)
    _login(client)
    page = client.get(f"/inbox/c/{conv_id}").text
    form = page.split('id="reply"')[1].split("</form>")[0]
    assert WINDOW_CLOSED_TEXT in form and "disabled" in form
    # A stale page that still posts is refused by the send layer.
    _, location = _form(client, f"/inbox/c/{conv_id}/reply", "text=hello")
    assert location.endswith("?error=window")
    assert fake_meta.requests == []
    assert WINDOW_CLOSED_TEXT in client.get(location).text


@pytest.mark.parametrize(("body", "error"), [("text=+++", "empty"), ("text=" + "a" * 4100, "long")])
def test_invalid_replies(
    client: TestClient, session_factory: sessionmaker[Session], body: str, error: str
) -> None:
    conv_id = _handed_off(session_factory)
    _login(client)
    assert _form(client, f"/inbox/c/{conv_id}/reply", body)[1].endswith(f"?error={error}")


def test_unknown_conversation(client: TestClient) -> None:
    _login(client)
    assert client.get("/inbox/c/999").status_code == 404


def test_upcoming_bookings_are_listed(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    from app.db.models import Booking

    with session_factory() as s:
        _, customer = make_conversation(s, name="Huda")
        start = datetime.now(UTC) + timedelta(days=2)
        s.add(
            Booking(
                customer_id=customer.id,
                service_id="whitening",
                staff_id="dr-sara",
                start_at=start,
                end_at=start + timedelta(hours=1),
            )
        )
        s.commit()
    _login(client)
    page = client.get("/inbox").text
    assert "Teeth whitening / تبييض الأسنان" in page and "Dr. Sara" in page and "Huda" in page


def test_reopen_with_template_when_window_closed(
    client: TestClient, session_factory: sessionmaker[Session], fake_meta: FakeMeta
) -> None:
    conv_id = _handed_off(session_factory, window_open=False)
    _login(client)
    page = client.get(f"/inbox/c/{conv_id}").text
    assert "Re-open with template" in page
    _, location = _form(client, f"/inbox/c/{conv_id}/reopen", "")
    assert location == f"/inbox/c/{conv_id}"
    [req] = fake_meta.requests
    template = req["template"]
    assert (template["name"], template["language"]["code"]) == ("conversation_reopen", "en")
    params = [p["text"] for p in template["components"][0]["parameters"]]
    assert params == ["Layla", "Ibtisama Demo Dental Clinic"]
    page = client.get(location).text
    assert "Re-open message sent" in page and "Re-open with template" not in page
    # Not twice while unanswered.
    assert _form(client, f"/inbox/c/{conv_id}/reopen", "")[1].endswith("?error=reopen")
    assert len(fake_meta.requests) == 1
    with session_factory() as s:
        msg = s.execute(select(Message).where(Message.type == "template")).scalar_one()
        assert msg.sender is Sender.STAFF and "Ibtisama" in (msg.body or "")


def test_reopen_refused_inside_window(
    client: TestClient, session_factory: sessionmaker[Session], fake_meta: FakeMeta
) -> None:
    conv_id = _handed_off(session_factory, window_open=True)
    _login(client)
    page = client.get(f"/inbox/c/{conv_id}").text
    assert 'id="window-closed" class="banner" hidden' in page  # shown only once it closes
    assert _form(client, f"/inbox/c/{conv_id}/reopen", "")[1].endswith("?error=reopen")
    assert fake_meta.requests == []


def test_booking_list_shows_reminder_state(
    client: TestClient, session_factory: sessionmaker[Session], settings: Settings
) -> None:
    from app.db.models import Booking
    from app.scheduler.reminders import sync_for_booking

    with session_factory() as s:
        _, customer = make_conversation(s, name="Huda")
        start = datetime.now(UTC) + timedelta(days=2)
        booking = Booking(
            customer_id=customer.id,
            service_id="whitening",
            start_at=start,
            end_at=start + timedelta(hours=1),
        )
        s.add(booking)
        s.flush()
        sync_for_booking(s, settings, booking, datetime.now(UTC))
        s.commit()
    _login(client)
    page = client.get("/inbox").text
    assert "24h before: due" in page and "2h before: due" in page
