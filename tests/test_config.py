"""Settings validation (CLAUDE.md §8: refuse to start if a required key is missing)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_missing_required_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSAPP_APP_SECRET")
    with pytest.raises(ValidationError, match="whatsapp_app_secret"):
        Settings(_env_file=None)


def test_missing_handoff_message_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HANDOFF_HOURS_MESSAGE")
    with pytest.raises(ValidationError, match="handoff_hours_message"):
        Settings(_env_file=None)


def test_defaults(settings: Settings) -> None:
    assert settings.default_language == "ar"
    assert settings.dialect == "gulf"
    assert settings.quiet_hours == "22:00-09:00"
    assert settings.business_timezone == "Asia/Riyadh"
    assert settings.graph_api_base_url == "https://graph.facebook.com/v23.0"


def test_secrets_not_in_repr(settings: Settings) -> None:
    assert "test-app-secret" not in repr(settings)
    assert "test-access-token" not in repr(settings)


def test_missing_template_name_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPLATE_REMINDER")
    with pytest.raises(ValidationError, match="template_reminder"):
        Settings(_env_file=None)


def test_handoff_contacts_required_and_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HANDOFF_CONTACTS", "+966 50 000 0000, 966500000001")
    assert Settings(_env_file=None).staff_numbers == ["966500000000", "966500000001"]
    monkeypatch.setenv("HANDOFF_CONTACTS", " , ")
    with pytest.raises(ValidationError, match="at least one staff"):
        Settings(_env_file=None)
    monkeypatch.setenv("HANDOFF_CONTACTS", "call-me")
    with pytest.raises(ValidationError, match="phone number"):
        Settings(_env_file=None)


def test_reminder_offsets_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMINDER_OFFSETS", "2h, 24h, 90m, 1d")
    assert Settings(_env_file=None).reminder_deltas == [
        timedelta(days=1),
        timedelta(hours=2),
        timedelta(minutes=90),
    ]
    monkeypatch.setenv("REMINDER_OFFSETS", "tomorrow")
    with pytest.raises(ValidationError, match="reminder offset"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["22:00", "25:00-09:00", "09:00-09:00"])
def test_bad_quiet_hours_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("QUIET_HOURS", value)
    with pytest.raises(ValidationError, match="quiet_hours"):
        Settings(_env_file=None)


def test_alert_emails_need_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALERT_EMAILS", "desk@example.com")
    with pytest.raises(ValidationError, match="SMTP_HOST"):
        Settings(_env_file=None)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "agent@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    assert Settings(_env_file=None).alert_email_list == ["desk@example.com"]


def test_retrieval_not_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_RETRIEVAL_ENABLED", "true")
    with pytest.raises(ValidationError, match="retrieval"):
        Settings(_env_file=None)


def test_unknown_language_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEFAULT_LANGUAGE", "fr")
    with pytest.raises(ValidationError, match="default_language"):
        Settings(_env_file=None)


def test_unknown_booking_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOKING_PROVIDER", "outlook")
    with pytest.raises(ValidationError, match="booking_provider"):
        Settings(_env_file=None)
