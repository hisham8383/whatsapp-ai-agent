"""Quiet hours (CLAUDE.md §5): no lead follow-ups at night.

``QUIET_HOURS`` is a local-time range such as ``22:00-09:00`` that may wrap
past midnight. A send that falls inside it moves to the end of the range
("the next morning"). Reminders are not shifted: they are anchored to the
appointment and their offsets were chosen by the client (order question Q8).
Staff alerts are not shifted either: a handoff needs a human now.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def in_quiet_hours(local: time, window: tuple[time, time]) -> bool:
    """True if the local clock time ``local`` is inside ``window`` (end exclusive)."""
    start, end = window
    if start < end:
        return start <= local < end
    return local >= start or local < end  # wraps past midnight


def shift_out_of_quiet(when: datetime, tz: ZoneInfo, window: tuple[time, time]) -> datetime:
    """Move ``when`` to the end of the quiet window if it falls inside it.

    Args:
        when: Aware datetime (any zone).
        tz: ``BUSINESS_TIMEZONE``.
        window: ``Settings.quiet_window``.

    Returns:
        ``when`` unchanged, or the first moment after the window, in UTC.
    """
    local = when.astimezone(tz)
    if not in_quiet_hours(local.time().replace(tzinfo=None), window):
        return when.astimezone(UTC)
    start, end = window
    day = local.date()
    # Before midnight in a wrapping window, the quiet period ends tomorrow morning.
    if start > end and local.time().replace(tzinfo=None) >= start:
        day += timedelta(days=1)
    return datetime.combine(day, end, tzinfo=tz).astimezone(UTC)
