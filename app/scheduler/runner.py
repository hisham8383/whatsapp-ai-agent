"""Scheduler process: once a minute, run every due task (CLAUDE.md §3, §5).

``python -m app scheduler``. Run exactly **one** replica (k8s ``replicas: 1``).
A second one would still never double-send, because every send claims its
row with ``FOR UPDATE SKIP LOCKED`` (``app/scheduler/sending.py``), but it
would duplicate the sync and retention work for nothing.

Each tick, in order:

1. Staff alerts: first, because a customer is waiting for a human. The worker
   normally sends them within a second of the handoff (``AlertJob``); this
   catches any it couldn't.
2. Lead follow-ups: plan (interest older than the delay -> ``follow_ups`` row),
   then send the due ones.
3. Reminders: send the due ones.
4. Booking sync, every ``BOOKING_SYNC_MINUTES`` (external adapters only).
5. Retention, once every 24 hours.

A failing task is logged and the others still run; the next tick retries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.config import BookingProviderName, Settings
from app.db.models import FollowUp, Reminder, StaffAlert
from app.db.session import session_scope
from app.logging import get_logger
from app.scheduler import followups
from app.scheduler.booking_sync import BookingSync
from app.scheduler.retention import RUN_EVERY, run_retention
from app.scheduler.sending import Handler, drain

log = get_logger(__name__)

TICK = timedelta(minutes=1)


class Scheduler:
    """Owns the per-tick task list. ``tick`` is what tests call; ``run_forever`` loops it."""

    def __init__(
        self,
        settings: Settings,
        *,
        alerts: Handler,
        follow_ups: Handler,
        reminders: Handler,
        sync: BookingSync | None,
        clock: Callable[[], datetime] | None = None,
        factory: sessionmaker[Session] | None = None,
    ) -> None:
        """
        Args:
            settings: Runtime config.
            alerts / follow_ups / reminders: Row handlers (``AlertSender``,
                ``FollowUpSender``, ``ReminderSender``).
            sync: Booking sync, or ``None`` for the internal adapter.
            clock: Returns "now" (UTC); tests pin it.
            factory: Session factory; tests pass one bound to SQLite.
        """
        self._settings = settings
        self._alerts, self._follow_ups, self._reminders = alerts, follow_ups, reminders
        self._sync = sync if settings.booking_provider is not BookingProviderName.INTERNAL else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._factory = factory
        self._next_sync: datetime | None = None
        self._next_retention: datetime | None = None

    def _task(self, name: str, fn: Callable[[], int]) -> None:
        try:
            count = fn()
        except Exception:
            log.exception("scheduler task %s failed", name, extra={"event": "task_failed"})
            return
        if count:
            log.info("scheduler %s: %d", name, count, extra={"event": name, "count": count})

    def _plan_follow_ups(self) -> int:
        with session_scope(self._factory) as session:
            return followups.plan_due(session, self._settings, self._clock())

    def _run_sync(self) -> int:
        now = self._clock()
        if self._sync is None or (self._next_sync is not None and now < self._next_sync):
            return 0
        self._next_sync = now + timedelta(minutes=self._settings.booking_sync_minutes)
        with session_scope(self._factory) as session:
            return self._sync.run(session, now)

    def _run_retention(self) -> int:
        now = self._clock()
        if self._next_retention is not None and now < self._next_retention:
            return 0
        self._next_retention = now + RUN_EVERY
        with session_scope(self._factory) as session:
            r = run_retention(session, self._settings.log_retention_days, now)
        return r.messages_blanked + r.summaries_blanked + r.events_deleted + r.sends_deleted

    def tick(self) -> None:
        """Run every task once, in priority order."""
        clock, factory = self._clock, self._factory
        self._task("alerts_sent", lambda: drain(StaffAlert, self._alerts, clock, factory))
        self._task("followups_planned", self._plan_follow_ups)
        self._task("followups_sent", lambda: drain(FollowUp, self._follow_ups, clock, factory))
        self._task("reminders_sent", lambda: drain(Reminder, self._reminders, clock, factory))
        self._task("bookings_synced", self._run_sync)
        self._task("retention", self._run_retention)

    def run_forever(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """Tick every ``TICK``, measured from the start of each tick."""
        log.info("scheduler started", extra={"event": "scheduler_start"})
        while True:
            started = time.monotonic()
            self.tick()
            sleep(max(0.0, TICK.total_seconds() - (time.monotonic() - started)))
