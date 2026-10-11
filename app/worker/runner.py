"""Worker process: the job loop (CLAUDE.md §3).

Runs as ``python -m app worker``, from the same image as the API. It claims
one job at a time from the Postgres queue and routes it to the handler
registered for its type. Several replicas can run side by side, because
``claim_next`` uses ``SKIP LOCKED``.

Each job runs in its own transaction, with the handler's work inside a
SAVEPOINT. If the handler raises, its partial writes are rolled back, and the
failure is recorded on the job (retry with backoff) in the same commit.

Timed work (reminders, follow-ups, staff alerts, booking sync, retention)
is not here: it belongs to the single scheduler process (``app/scheduler/``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.logging import get_logger
from app.worker import queue

log = get_logger(__name__)

JobHandler = Callable[[Session, dict[str, Any]], None]


class Worker:
    """Pulls jobs from the queue and dispatches them by type."""

    def __init__(self, handlers: Mapping[str, JobHandler]) -> None:
        """
        Args:
            handlers: Job type → handler. Jobs of other types are left in the queue.
        """
        self._handlers = dict(handlers)

    def run_once(self, factory: sessionmaker[Session] | None = None) -> bool:
        """Claim and process a single job.

        Args:
            factory: Session factory. Tests pass one bound to SQLite.

        Returns:
            False if there was nothing to do, True otherwise.

        Failure modes:
            A handler exception is logged and the job is rescheduled with
            backoff (``queue.fail``). It never crashes the loop.
        """
        with session_scope(factory) as session:
            job = queue.claim_next(session, list(self._handlers))
            if job is None:
                return False
            handler = self._handlers[job.type]
            try:
                with session.begin_nested():
                    handler(session, job.payload)
            except Exception as exc:
                log.exception("job failed", extra={"job_id": job.id, "attempt": job.attempts})
                queue.fail(session, job, f"{type(exc).__name__}: {exc}")
            else:
                queue.complete(session, job)
        return True

    def run_forever(
        self, factory: sessionmaker[Session] | None = None, idle_sleep: float = 1.0
    ) -> None:
        """Poll the queue until the process is stopped.

        Args:
            factory: Session factory, or ``None`` for the process default.
            idle_sleep: Seconds to wait when the queue is empty.
        """
        log.info("worker started")
        while True:
            if not self.run_once(factory):
                time.sleep(idle_sleep)
