"""Local-time formatting and parsing for the model and the slot list."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.conversation.timefmt import context_now, for_model, parse_local, slot_label

RIYADH = ZoneInfo("Asia/Riyadh")
T = datetime(2026, 10, 5, 7, 30, tzinfo=UTC)  # Monday 10:30 in Riyadh


def test_model_format_round_trip() -> None:
    assert for_model(T, RIYADH) == "2026-10-05 10:30"
    assert parse_local("2026-10-05 10:30", RIYADH) == T
    assert parse_local("2026-10-05T10:30", RIYADH) == T
    assert context_now(T, RIYADH) == "Monday 2026-10-05 10:30"


@pytest.mark.parametrize("bad", ["tomorrow 10:30", "2026-13-05 10:30", "05/10/2026 10:30"])
def test_parse_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_local(bad, RIYADH)


def test_slot_labels_fit_whatsapp_rows() -> None:
    assert slot_label(T, RIYADH, "en") == "Mon 5 Oct, 10:30"
    assert slot_label(T, RIYADH, "ar") == "الاثنين 5 أكتوبر 10:30"
    wednesday = datetime(2026, 9, 30, 18, 30, tzinfo=UTC)
    assert len(slot_label(wednesday, RIYADH, "ar")) <= 24
