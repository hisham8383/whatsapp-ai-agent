"""Staff alerts on handoff: WhatsApp template + optional email (CLAUDE.md §5)."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import ClassVar, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.alerts.email import EmailSender
from app.alerts.service import AlertJob, AlertSender, queue_handoff_alerts, reason_label
from app.config import Settings
from app.conversation.tools import hand_off
from app.db.models import AlertChannel, Conversation, ConversationState, SendStatus, StaffAlert
from app.http import ExternalServiceError
from app.scheduler.sending import drain
from app.whatsapp.client import WhatsAppClient
from app.worker.queue import JOB_SEND_ALERTS
from app.worker.runner import Worker
from tests.conftest import NOW, FakeMeta, make_conversation


class FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []
    fail_times: ClassVar[int] = 0

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        if FakeSMTP.fail_times:
            FakeSMTP.fail_times -= 1
            raise smtplib.SMTPServerDisconnected("gone")

    def send_message(self, msg: EmailMessage) -> None:
        FakeSMTP.sent.append(msg)


SMTP = cast(Callable[..., smtplib.SMTP], FakeSMTP)


@pytest.fixture
def email_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in {
        "alert_emails": "desk@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_from": "agent@example.com",
        "public_base_url": "https://agent.example.com",
    }.items():
        monkeypatch.setattr(settings, key, value)
    FakeSMTP.sent, FakeSMTP.fail_times = [], 0
    return settings


def _handoff(factory: sessionmaker[Session], settings: Settings) -> int:
    with factory() as s:
        conv, _ = make_conversation(s, name="Layla Q")
        hand_off(conv, "complaint", "Unhappy with her last cleaning; wants a call back.")
        queue_handoff_alerts(s, settings, conv, NOW)
        s.commit()
        return conv.id


def test_one_row_per_contact(
    session_factory: sessionmaker[Session], email_settings: Settings
) -> None:
    _handoff(session_factory, email_settings)
    with session_factory() as s:
        rows = s.execute(select(StaffAlert.channel, StaffAlert.contact)).all()
    assert sorted(rows) == [
        (AlertChannel.EMAIL, "desk@example.com"),
        (AlertChannel.WHATSAPP, "966500000900"),
    ]


def test_whatsapp_and_email_sent(
    session_factory: sessionmaker[Session],
    email_settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    conv_id = _handoff(session_factory, email_settings)
    sender = AlertSender(email_settings, wa_client, EmailSender(email_settings, SMTP))
    assert drain(StaffAlert, sender, lambda: NOW, session_factory) == 2
    [req] = fake_meta.requests
    assert req["to"] == "966500000900"
    template = req["template"]
    assert (template["name"], template["language"]["code"]) == ("staff_handoff_alert", "ar")
    params = [p["text"] for p in template["components"][0]["parameters"]]
    assert params == ["Layla", "شكوى"]
    [mail] = FakeSMTP.sent
    body = mail.get_content()
    assert "Layla" in body and "wants a call back" in body
    assert f"https://agent.example.com/inbox/c/{conv_id}" in body
    assert "966500000001" not in body  # no customer phone number in email (§2)
    with session_factory() as s:
        assert set(s.execute(select(StaffAlert.status)).scalars()) == {SendStatus.SENT}


def test_skipped_when_already_back_with_bot(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    conv_id = _handoff(session_factory, settings)
    with session_factory() as s:
        conv = s.get(Conversation, conv_id)
        assert conv is not None
        conv.state = ConversationState.BOT
        s.commit()
    drain(StaffAlert, AlertSender(settings, wa_client), lambda: NOW, session_factory)
    assert fake_meta.requests == []


def test_smtp_retries_then_raises(email_settings: Settings) -> None:
    FakeSMTP.fail_times = 1
    EmailSender(email_settings, SMTP, sleep=lambda _s: None).send("a@b.co", "s", "b")
    assert len(FakeSMTP.sent) == 1
    FakeSMTP.fail_times = 5
    with pytest.raises(ExternalServiceError, match="smtp"):
        EmailSender(email_settings, SMTP, sleep=lambda _s: None).send("a@b.co", "s", "b")


def test_reason_labels() -> None:
    assert reason_label("asked_for_human", "en") == "asked for a person"
    assert reason_label("nonsense", "ar") == "يحتاج مساعدة من الفريق"


def test_worker_sends_alert_right_after_handoff(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    """Seen live: with only the scheduler (every minute), staff clicked Return to bot
    before the next tick and the alert was skipped. The worker now sends it at once."""
    _handoff(session_factory, settings)  # queues rows + the send job, like the handler
    worker = Worker(
        {JOB_SEND_ALERTS: AlertJob(AlertSender(settings, wa_client), clock=lambda: NOW).handle}
    )
    assert worker.run_once(session_factory)
    assert [r["template"]["name"] for r in fake_meta.requests] == ["staff_handoff_alert"]
    with session_factory() as s:
        assert s.execute(select(StaffAlert.status)).scalar_one() is SendStatus.SENT
    # The scheduler's fallback pass finds nothing left: no double send.
    assert drain(StaffAlert, AlertSender(settings, wa_client), lambda: NOW, session_factory) == 0
    assert len(fake_meta.requests) == 1


def test_alert_job_leaves_already_sent_alerts_alone(
    session_factory: sessionmaker[Session],
    settings: Settings,
    wa_client: WhatsAppClient,
    fake_meta: FakeMeta,
) -> None:
    conv_id = _handoff(session_factory, settings)
    drain(
        StaffAlert, AlertSender(settings, wa_client), lambda: NOW, session_factory
    )  # scheduler first
    with session_factory() as s:
        AlertJob(AlertSender(settings, wa_client), clock=lambda: NOW).handle(
            s, {"conversation_id": conv_id}
        )
        s.commit()
    assert len(fake_meta.requests) == 1
