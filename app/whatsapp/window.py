"""The 24-hour customer-service window guard (CLAUDE.md §4).

WhatsApp policy: a business may send **free-form** messages (text, interactive)
only within 24 hours of the customer's last inbound message. Outside that
window, only Meta-approved **template** messages are allowed. Meta rejects
non-compliant sends (error 131047), and repeated violations can hurt the
number's quality rating, so we refuse them before calling the API.

Outside the window the business can only write first with an approved
template (``WhatsAppClient.send_template``): reminders, follow-ups, staff
alerts, and the inbox's "Re-open with template" button, which replaces the
reply box when ``is_window_open`` is False.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

SERVICE_WINDOW = timedelta(hours=24)


class OutsideServiceWindowError(Exception):
    """Raised when a send is attempted outside the 24h window. The customer must write first."""


def _as_utc(value: datetime) -> datetime:
    # SQLite (tests) returns naive datetimes, and we always store UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_window_open(last_customer_msg_at: datetime | None, now: datetime | None = None) -> bool:
    """Return True if free-form messages are currently allowed.

    Args:
        last_customer_msg_at: When the customer last wrote to us, or ``None`` if never.
        now: Injectable clock for tests.
    """
    if last_customer_msg_at is None:
        return False
    now = now or datetime.now(UTC)
    return now - _as_utc(last_customer_msg_at) < SERVICE_WINDOW


def ensure_window_open(last_customer_msg_at: datetime | None, now: datetime | None = None) -> None:
    """Raise ``OutsideServiceWindowError`` if a free-form send is not allowed right now."""
    if not is_window_open(last_customer_msg_at, now):
        raise OutsideServiceWindowError(
            "24h customer-service window is closed; the customer must message again"
        )
