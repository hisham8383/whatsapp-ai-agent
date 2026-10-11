"""Application settings, loaded and validated once at startup.

Every key listed in ``.env.example`` (CLAUDE.md §8) is declared here. Values
come from environment variables. In production those are the ConfigMap
(non-secret) and the secret store (secrets). For local development only, a
``.env`` file is also read.

Why this module is strict:
    * Required keys have no defaults, so a missing key raises
      ``pydantic.ValidationError`` and the process refuses to start. It never
      runs half-configured against a customer's WhatsApp number.
    * Secrets are typed ``SecretStr``, so they print as ``**********`` in reprs,
      tracebacks, and logs (CLAUDE.md §2: "never log secrets").
"""

from __future__ import annotations

import re
from datetime import time, timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_OFFSET = re.compile(r"^(\d+)\s*([mhd])$")
_UNIT = {"m": "minutes", "h": "hours", "d": "days"}
_CLOCK_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def split_list(value: str) -> list[str]:
    """Split a comma-separated env value, dropping blanks."""
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_offsets(value: str) -> list[timedelta]:
    """Parse ``REMINDER_OFFSETS`` such as ``"24h,2h"`` or ``"1d,90m"``.

    Returns:
        The offsets, largest first, without duplicates.

    Raises:
        ValueError: An entry isn't ``<number><m|h|d>``, or is zero.
    """
    out: set[timedelta] = set()
    for part in split_list(value):
        m = _OFFSET.match(part.lower())
        if not m or int(m.group(1)) == 0:
            raise ValueError(f"reminder offset must look like 24h, 90m or 1d, got {part!r}")
        out.add(timedelta(**{_UNIT[m.group(2)]: int(m.group(1))}))
    return sorted(out, reverse=True)


def parse_clock_range(value: str) -> tuple[time, time]:
    """Parse ``QUIET_HOURS`` such as ``"22:00-09:00"``. The range may wrap past midnight.

    Raises:
        ValueError: Not ``HH:MM-HH:MM``, an impossible clock time, or start == end.
    """
    m = _CLOCK_RANGE.match(value.strip())
    if not m:
        raise ValueError(f"expected HH:MM-HH:MM, got {value!r}")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    start, end = time(h1, m1), time(h2, m2)  # time() raises ValueError on 25:00
    if start == end:
        raise ValueError("quiet hours start and end must differ")
    return start, end


def normalize_wa_number(value: str) -> str:
    """``"+966 50 000 0000"`` -> ``"966500000000"``, the digits-only form the Cloud API uses.

    Raises:
        ValueError: Not 8-15 digits once ``+``, spaces, dashes and brackets are removed (E.164).
    """
    digits = re.sub(r"[\s+\-()]", "", value)
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise ValueError(f"not an international phone number: {value!r}")
    return digits


class BookingProviderName(StrEnum):
    """Which booking adapter to load (CLAUDE.md §6)."""

    GOOGLE_CALENDAR = "google_calendar"
    CALENDLY = "calendly"
    GENERIC_API = "generic_api"
    INTERNAL = "internal"


class LLMProviderName(StrEnum):
    """Which LLM adapter to load (CLAUDE.md §3)."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


class Settings(BaseSettings):
    """All runtime configuration. See CLAUDE.md §8 for where each value comes from."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- Non-secret (ConfigMap) ----------
    whatsapp_phone_number_id: str
    whatsapp_business_account_id: str
    meta_graph_api_version: str = "v23.0"

    business_timezone: str = "Asia/Riyadh"
    # Used when a message has no language signal (e.g. a voice note or an emoji).
    default_language: Literal["ar", "en"] = "ar"
    tone: str = "friendly"
    # Arabic register (§5): "gulf" or "saudi" -> natural Gulf/Saudi phrasing per
    # knowledge/style_ar.md; "msa" -> Modern Standard Arabic throughout.
    dialect: Literal["gulf", "saudi", "msa"] = "gulf"
    assistant_name: str
    # No reminder or follow-up goes out inside this local-time range; sends shift to
    # its end. It may wrap past midnight, e.g. "22:00-09:00" (§5).
    quiet_hours: str = "22:00-09:00"

    business_config_path: str = "./knowledge/business.yaml"
    faq_path: str = "./knowledge/faqs.yaml"
    style_guide_path: str = "./knowledge/style_ar.md"
    # Retrieval (pgvector) for knowledge bases above ~15k tokens (§5). Not built in
    # this release: the whole knowledge base goes into the cached system prompt.
    kb_retrieval_enabled: bool = False

    booking_provider: BookingProviderName
    calendar_id: str = ""
    booking_api_base_url: str = ""
    # How often the scheduler pulls bookings made outside the agent (walk-ins,
    # phone), so their reminders stay correct (§6). Unused by the internal provider.
    booking_sync_minutes: int = Field(default=5, ge=1, le=1440)

    # Staff WhatsApp numbers alerted on every handoff, comma-separated (§5).
    handoff_contacts: str
    # Optional email alerts on handoff, comma-separated. Needs the SMTP_* keys.
    alert_emails: str = ""
    # Public URL of this deployment, e.g. "https://agent.example.com". Only used for
    # the inbox link in alert emails; the WhatsApp alert template has no link slot.
    public_base_url: str = ""

    # Told to the customer after a handoff (§5), e.g. "Our team replies Sun-Thu, 9am-9pm."
    # Bilingual form: "<Arabic> || <English>". A single text is used for both languages.
    handoff_hours_message: str
    handoff_auto_release_hours: int = Field(default=24, ge=1)

    # Reminder send times before each appointment, e.g. "24h,2h" (§5, order question Q8).
    reminder_offsets: str = "24h,2h"
    # One lead follow-up goes out this long after the customer showed interest
    # without booking, at most once per customer per cooldown (§5).
    followup_delay_hours: int = Field(default=20, ge=1, le=24 * 14)
    followup_cooldown_days: int = Field(default=14, ge=1, le=365)

    # Approved template names (§4). All required: every feature is always on.
    template_reminder: str = Field(min_length=1)
    template_booking_update: str = Field(min_length=1)
    template_staff_alert: str = Field(min_length=1)
    template_reopen: str = Field(min_length=1)
    template_followup: str = Field(min_length=1)

    log_retention_days: int = Field(default=90, ge=1)

    # Optional. For scale-to-zero hosting (e.g. a Cloud Run worker with min 0
    # instances): the API pings this URL after queueing a message so the worker
    # starts. Leave empty when the worker is always on (the default).
    worker_wake_url: str = ""

    llm_provider: LLMProviderName = LLMProviderName.ANTHROPIC
    llm_model: str = "claude-haiku-4-5-20251001"
    llm_max_history_messages: int = Field(default=20, ge=1, le=200)
    llm_max_output_tokens: int = Field(default=400, ge=50, le=4000)

    # Email alerts only (ALERT_EMAILS). SMTP_FROM doubles as the login user name.
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_from: str = ""

    # ---------- Secrets (secret store only) ----------
    whatsapp_access_token: SecretStr
    whatsapp_app_secret: SecretStr
    webhook_verify_token: SecretStr
    llm_api_key: SecretStr
    calendar_credentials: SecretStr
    database_url: SecretStr
    inbox_auth_secret: SecretStr
    embeddings_api_key: SecretStr = SecretStr("")  # only with KB_RETRIEVAL_ENABLED
    smtp_password: SecretStr = SecretStr("")  # only with ALERT_EMAILS

    @field_validator("quiet_hours")
    @classmethod
    def _valid_quiet_hours(cls, value: str) -> str:
        parse_clock_range(value)
        return value

    @field_validator("reminder_offsets")
    @classmethod
    def _valid_offsets(cls, value: str) -> str:
        if not parse_offsets(value):
            raise ValueError("at least one reminder offset is required, e.g. 24h")
        return value

    @field_validator("handoff_contacts")
    @classmethod
    def _valid_contacts(cls, value: str) -> str:
        if not split_list(value):
            raise ValueError("at least one staff WhatsApp number is required for handoff alerts")
        for number in split_list(value):
            normalize_wa_number(number)
        return value

    @field_validator("alert_emails")
    @classmethod
    def _valid_emails(cls, value: str) -> str:
        bad = [e for e in split_list(value) if not _EMAIL.match(e)]
        if bad:
            raise ValueError(f"not email addresses: {bad}")
        return value

    @field_validator("kb_retrieval_enabled")
    @classmethod
    def _retrieval_not_built(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "retrieval isn't built in this release; keep KB_RETRIEVAL_ENABLED=false "
                "(the knowledge base goes into the cached system prompt, CLAUDE.md §5)"
            )
        return value

    @model_validator(mode="after")
    def _email_needs_smtp(self) -> Settings:
        """Email alerts without a mail server would only fail at the first handoff."""
        if self.alert_email_list:
            missing = [
                key
                for key, value in (
                    ("SMTP_HOST", self.smtp_host),
                    ("SMTP_FROM", self.smtp_from),
                    ("SMTP_PASSWORD", self.smtp_password.get_secret_value()),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"ALERT_EMAILS is set, so these are required: {missing}")
        return self

    @field_validator("business_timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        """Reject a time zone name the IANA database doesn't know, at startup."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        """``BUSINESS_TIMEZONE`` as a ``ZoneInfo``. Times are stored in UTC and shown in this."""
        return ZoneInfo(self.business_timezone)

    def handoff_hours_for(self, language: str) -> str:
        """The staff-hours sentence in ``language`` ("ar" or "en").

        ``HANDOFF_HOURS_MESSAGE`` may hold both languages as ``"<Arabic> || <English>"``.
        Without the separator, the same text is used for both.
        """
        parts = [p.strip() for p in self.handoff_hours_message.split("||")]
        if len(parts) == 2:
            return parts[0] if language == "ar" else parts[1]
        return self.handoff_hours_message.strip()

    @property
    def quiet_window(self) -> tuple[time, time]:
        """``QUIET_HOURS`` as (start, end) local clock times."""
        return parse_clock_range(self.quiet_hours)

    @property
    def reminder_deltas(self) -> list[timedelta]:
        """``REMINDER_OFFSETS`` as timedeltas, largest first."""
        return parse_offsets(self.reminder_offsets)

    @property
    def staff_numbers(self) -> list[str]:
        """``HANDOFF_CONTACTS`` in the digits-only WhatsApp form."""
        return [normalize_wa_number(n) for n in split_list(self.handoff_contacts)]

    @property
    def alert_email_list(self) -> list[str]:
        """``ALERT_EMAILS`` as a list; empty when email alerts are off."""
        return split_list(self.alert_emails)

    @property
    def graph_api_base_url(self) -> str:
        """Base URL for Meta Graph API calls, pinned to the configured version."""
        return f"https://graph.facebook.com/{self.meta_graph_api_version}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, validating them on first call.

    Raises:
        pydantic.ValidationError: A required key is missing or invalid. This is
            deliberate, so the app refuses to start (CLAUDE.md §8).
    """
    return Settings()  # values come from the environment
