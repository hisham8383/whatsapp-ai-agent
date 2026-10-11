"""Log redaction (CLAUDE.md §2, §9: no full phone numbers in logs)."""

from __future__ import annotations

import json
import logging

from app.logging import JsonFormatter, redact_phone


def test_redact_phone() -> None:
    assert redact_phone("966501234567") == "9665******67"
    assert redact_phone("+966501234567") == "+9665******67"
    assert redact_phone("12345") == "*****"


def test_formatter_scrubs_numbers_in_message() -> None:
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "send to 966501234567 failed", None, None
    )
    record.conversation_id = 7
    out = json.loads(JsonFormatter().format(record))
    assert "966501234567" not in out["msg"]
    assert out["conversation_id"] == 7
