"""Structured JSON logging with PII redaction.

CLAUDE.md §9: logs are JSON, carry the conversation id, and never include
secrets or full phone numbers. Saudi PDPL data minimization applies (§2): a
WhatsApp number is personal data, so only a masked form reaches the logs.

Usage::

    log = get_logger(__name__)
    log.info("message stored", extra={"conversation_id": 42, "wa_id": redact_phone(wa_id)})
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Fields callers may pass via ``extra=`` that we copy into the JSON record.
_EXTRA_FIELDS = (
    "conversation_id",
    "customer_id",
    "job_id",
    "wa_message_id",
    "wa_id",
    "event",
    "status",
    "error_code",
    "attempt",
    "duration_ms",
    "count",
)

# A run of 8+ digits, optionally with a leading "+". Masks phone numbers that
# slip into free-text messages (e.g. exception strings) as a safety net.
_PHONE_RE = re.compile(r"\+?\d{8,15}")


def redact_phone(number: str) -> str:
    """Mask a phone number, keeping only a short prefix and the last 2 digits.

    Args:
        number: A phone number / WhatsApp id such as ``"966501234567"``.

    Returns:
        A masked form such as ``"9665*******67"``. Short inputs are masked fully.
    """
    digits = number.lstrip("+")
    plus = "+" if number.startswith("+") else ""
    if len(digits) <= 6:
        return plus + "*" * len(digits)
    return f"{plus}{digits[:4]}{'*' * (len(digits) - 6)}{digits[-2:]}"


def _scrub(text: str) -> str:
    return _PHONE_RE.sub(lambda m: redact_phone(m.group(0)), text)


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _scrub(record.getMessage()),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc"] = _scrub(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO. Keep them quieter.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger. The formatter is installed by ``configure_logging``."""
    return logging.getLogger(name)
