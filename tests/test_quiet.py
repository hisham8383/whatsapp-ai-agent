"""Quiet hours (CLAUDE.md §5): follow-ups move to the next morning."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.scheduler.quiet import in_quiet_hours, shift_out_of_quiet

RIYADH = ZoneInfo("Asia/Riyadh")
NIGHT = (time(22, 0), time(9, 0))


@pytest.mark.parametrize(
    ("clock", "quiet"),
    [
        (time(21, 59), False),
        (time(22, 0), True),
        (time(2, 0), True),
        (time(8, 59), True),
        (time(9, 0), False),
        (time(14, 0), False),
    ],
)
def test_wrapping_window(clock: time, quiet: bool) -> None:
    assert in_quiet_hours(clock, NIGHT) is quiet


def test_same_day_window() -> None:
    assert in_quiet_hours(time(13, 30), (time(13, 0), time(14, 0)))
    assert not in_quiet_hours(time(14, 0), (time(13, 0), time(14, 0)))


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=RIYADH)


def test_late_evening_moves_to_next_morning() -> None:
    shifted = shift_out_of_quiet(local(2026, 10, 4, 23, 15), RIYADH, NIGHT)
    assert shifted == local(2026, 10, 5, 9, 0).astimezone(UTC)


def test_early_morning_moves_to_same_morning() -> None:
    shifted = shift_out_of_quiet(local(2026, 10, 5, 3, 0), RIYADH, NIGHT)
    assert shifted == local(2026, 10, 5, 9, 0).astimezone(UTC)


def test_daytime_unchanged() -> None:
    when = local(2026, 10, 5, 15, 0)
    assert shift_out_of_quiet(when, RIYADH, NIGHT) == when.astimezone(UTC)
