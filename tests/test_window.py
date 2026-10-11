"""24h customer-service window guard (CLAUDE.md §4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.whatsapp.window import is_window_open

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_open_within_24h() -> None:
    assert is_window_open(NOW - timedelta(hours=23, minutes=59), now=NOW)


def test_closed_at_24h_and_after() -> None:
    assert not is_window_open(NOW - timedelta(hours=24), now=NOW)
    assert not is_window_open(NOW - timedelta(days=3), now=NOW)


def test_closed_when_customer_never_wrote() -> None:
    assert not is_window_open(None, now=NOW)


def test_naive_datetime_treated_as_utc() -> None:
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    assert is_window_open(naive, now=NOW)
