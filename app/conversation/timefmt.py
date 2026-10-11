"""Date and time formatting for the model and for customers.

The model always sees and writes local times as ``YYYY-MM-DD HH:MM`` in
``BUSINESS_TIMEZONE`` (unambiguous, easy to copy into a tool call). Customers
see short localized labels in the interactive slot list.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

MODEL_FORMAT = "%Y-%m-%d %H:%M"
_PARSE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::\d{2})?\s*$")

_DAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAYS_AR = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد")
_MONTHS_AR = (
    "يناير",
    "فبراير",
    "مارس",
    "أبريل",
    "مايو",
    "يونيو",
    "يوليو",
    "أغسطس",
    "سبتمبر",
    "أكتوبر",
    "نوفمبر",
    "ديسمبر",
)


def for_model(value: datetime, tz: ZoneInfo) -> str:
    """``2026-10-05 10:30`` in local time."""
    return value.astimezone(tz).strftime(MODEL_FORMAT)


def context_now(value: datetime, tz: ZoneInfo) -> str:
    """``Sunday 2026-10-04 09:00`` in local time, for the per-message context line."""
    local = value.astimezone(tz)
    return f"{local.strftime('%A')} {local.strftime(MODEL_FORMAT)}"


def parse_local(text: str, tz: ZoneInfo) -> datetime:
    """Parse ``YYYY-MM-DD HH:MM`` (local) into an aware UTC datetime.

    Raises:
        ValueError: Not in that format, or not a real date/time.
    """
    m = _PARSE.match(text)
    if not m:
        raise ValueError(f"expected 'YYYY-MM-DD HH:MM', got {text!r}")
    y, mo, d, h, mi = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


def parse_day(text: str) -> date:
    """Parse ``YYYY-MM-DD``.

    Raises:
        ValueError: Not a valid date in that format.
    """
    return date.fromisoformat(text.strip()[:10])


_DAYS_EN_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS_EN_LONG = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def long_label(value: datetime, tz: ZoneInfo, language: str) -> str:
    """``Monday 5 October at 10:00`` / ``يوم الاثنين 5 أكتوبر الساعة 10:00``, for messages."""
    local = value.astimezone(tz)
    hm = local.strftime("%H:%M")
    if language == "ar":
        return (
            f"يوم {_DAYS_AR[local.weekday()]} {local.day} {_MONTHS_AR[local.month - 1]} الساعة {hm}"
        )
    return (
        f"{_DAYS_EN_LONG[local.weekday()]} {local.day} {_MONTHS_EN_LONG[local.month - 1]} at {hm}"
    )


def slot_label(value: datetime, tz: ZoneInfo, language: str) -> str:
    """Short list-row title (Meta allows 24 characters).

    ``Mon 5 Oct, 10:30`` in English, ``الاثنين 5 أكتوبر 10:30`` in Arabic.
    """
    local = value.astimezone(tz)
    hm = local.strftime("%H:%M")
    if language == "ar":
        return f"{_DAYS_AR[local.weekday()]} {local.day} {_MONTHS_AR[local.month - 1]} {hm}"
    return f"{_DAYS_EN[local.weekday()]} {local.day} {_MONTHS_EN[local.month - 1]}, {hm}"


def day_label(value: datetime, tz: ZoneInfo, language: str) -> str:
    """``Monday 5 October`` / ``الاثنين 5 أكتوبر``: the day parameter of templates."""
    local = value.astimezone(tz)
    if language == "ar":
        return f"{_DAYS_AR[local.weekday()]} {local.day} {_MONTHS_AR[local.month - 1]}"
    return f"{_DAYS_EN_LONG[local.weekday()]} {local.day} {_MONTHS_EN_LONG[local.month - 1]}"


def time_label(value: datetime, tz: ZoneInfo) -> str:
    """``17:30``: the time parameter of templates (24-hour, read the same in both languages)."""
    return value.astimezone(tz).strftime("%H:%M")
