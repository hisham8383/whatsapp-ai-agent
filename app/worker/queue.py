"""Postgres-backed job queue (CLAUDE.md §3).

The webhook enqueues and returns 200 immediately. The worker claims jobs and
does the slow work (LLM calls, sends). Keeping the queue in Postgres avoids
running Redis for the client's expected load.

Concurrency: ``claim_next`` uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so that
several worker replicas never pick the same job. (SQLite, used in unit tests,
ignores the locking clause. Multi-replica behavior is only guaranteed on Postgres.)

The public surface is ``enqueue`` / ``claim_next`` / ``complete`` / ``fail``,
which is small enough to swap for SQS or Pub/Sub later without touching callers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobStatus, utcnow

# Job types. Each maps to a handler registered in ``app/__main__.py``.
JOB_HANDLE_INBOUND = "handle_inbound"
# Sends a handoff's staff alerts right away (app/alerts/service.py:AlertJob).
JOB_SEND_ALERTS = "send_staff_alerts"

MAX_ATTEMPTS = 5
# A job locked longer than this is assumed to belong to a crashed worker and is reclaimed.
STALE_LOCK_AFTER = timedelta(minutes=5)
# Backoff cap, so a failing job is retried at least every 10 minutes until it gives up.
MAX_BACKOFF_SECONDS = 600


def enqueue(
    session: Session, job_type: str, payload: dict[str, Any], run_at: datetime | None = None
) -> Job:
    """Add a job. It's committed with the caller's transaction.

    Enqueueing in the same transaction that stores the message means that
    either both happen or neither does. There's no "stored but never processed" gap.

    Args:
        session: Open session. The caller commits.
        job_type: One of the ``JOB_*`` constants.
        payload: JSON-serializable job arguments. Keep ids here, not message text,
            so PDPL retention only has to clean the ``messages`` table.
        run_at: Earliest time to run. Defaults to now.

    Returns:
        The new (flushed) job.
    """
    job = Job(type=job_type, payload=payload, run_at=run_at or utcnow())
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session, job_types: list[str] | None = None) -> Job | None:
    """Lock and return the next runnable job, or ``None`` if the queue is empty.

    A job is runnable if it is queued and due, or if it is running but its lock
    is stale (the worker died mid-job). The row lock is held until the caller's
    transaction ends, so no other replica can claim the same job meanwhile.

    Args:
        session: Open session. The caller commits.
        job_types: Restrict to these types, or ``None`` for any type.
    """
    now = utcnow()
    stmt = (
        select(Job)
        .where(
            ((Job.status == JobStatus.QUEUED) & (Job.run_at <= now))
            | ((Job.status == JobStatus.RUNNING) & (Job.locked_at < now - STALE_LOCK_AFTER))
        )
        .order_by(Job.run_at, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job_types:
        stmt = stmt.where(Job.type.in_(job_types))
    job = session.execute(stmt).scalar_one_or_none()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.locked_at = now
    job.attempts += 1
    session.flush()
    return job


def complete(session: Session, job: Job) -> None:
    """Mark a job done."""
    job.status = JobStatus.DONE
    job.locked_at = None
    job.last_error = None


def fail(session: Session, job: Job, error: str) -> None:
    """Record a failure and reschedule with exponential backoff, or give up after MAX_ATTEMPTS.

    Args:
        session: Open session. The caller commits.
        job: The job that failed.
        error: Short description, stored (truncated) in ``last_error``. It must
            not contain secrets or full phone numbers.
    """
    job.locked_at = None
    job.last_error = error[:2000]
    if job.attempts >= MAX_ATTEMPTS:
        job.status = JobStatus.FAILED
        return
    job.status = JobStatus.QUEUED
    job.run_at = utcnow() + timedelta(seconds=min(2**job.attempts * 5, MAX_BACKOFF_SECONDS))
