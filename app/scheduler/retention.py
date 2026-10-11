"""Daily retention task (CLAUDE.md §7, Saudi PDPL data minimization §2).

Run once a day by the scheduler (``app/scheduler/runner.py``). Each run:

* blanks message text older than ``LOG_RETENTION_DAYS`` (the row stays, so
  delivery statuses and counts survive, but the conversation text is gone);
* blanks handoff summaries of conversations idle for that long (they quote
  the customer);
* deletes ``processed_events`` older than 7 days (Meta stops retrying long
  before that, so older ids can't be duplicates any more);
* deletes finished jobs older than 7 days (housekeeping; payloads hold ids only);
* deletes finished reminders, follow-ups and staff alerts older than 90 days
  (§7), and lead interests decided that long ago.

Customers (with their opt-out flags) and bookings are kept: they are business
records the client needs, and an opt-out must outlive any retention period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, update
from sqlalchemy.orm import Session

from app.db.models import (
    Conversation,
    FollowUp,
    Job,
    JobStatus,
    LeadInterest,
    Message,
    ProcessedEvent,
    Reminder,
    SendStatus,
    StaffAlert,
    utcnow,
)
from app.logging import get_logger

log = get_logger(__name__)

EVENT_TTL = timedelta(days=7)
JOB_TTL = timedelta(days=7)
SEND_TTL = timedelta(days=90)  # §7: sent reminders/follow-ups older than 90 days
RUN_EVERY = timedelta(hours=24)
_FINISHED = (SendStatus.SENT, SendStatus.FAILED, SendStatus.CANCELLED, SendStatus.SKIPPED)


@dataclass(slots=True)
class RetentionResult:
    messages_blanked: int
    summaries_blanked: int
    events_deleted: int
    jobs_deleted: int
    sends_deleted: int = 0


def run_retention(
    session: Session, retention_days: int, now: datetime | None = None
) -> RetentionResult:
    """Apply the retention rules once.

    Args:
        session: Open session. The caller commits.
        retention_days: ``LOG_RETENTION_DAYS``.
        now: Injectable clock for tests.

    Returns:
        How many rows each rule touched, for the log.
    """
    now = now or utcnow()
    cutoff = now - timedelta(days=retention_days)
    messages = _rows(
        session,
        update(Message)
        .where(Message.created_at < cutoff, Message.body.is_not(None))
        .values(body=None),
    )
    summaries = _rows(
        session,
        update(Conversation)
        .where(Conversation.updated_at < cutoff, Conversation.handoff_summary.is_not(None))
        .values(handoff_summary=None),
    )
    events = _rows(
        session, delete(ProcessedEvent).where(ProcessedEvent.processed_at < now - EVENT_TTL)
    )
    jobs = _rows(
        session,
        delete(Job).where(
            Job.status.in_([JobStatus.DONE, JobStatus.FAILED]), Job.run_at < now - JOB_TTL
        ),
    )
    old = now - SEND_TTL
    sends = sum(
        _rows(session, delete(model).where(model.status.in_(_FINISHED), model.created_at < old))
        for model in (Reminder, FollowUp, StaffAlert)
    )
    sends += _rows(
        session,
        delete(LeadInterest).where(
            LeadInterest.handled_at.is_not(None), LeadInterest.handled_at < old
        ),
    )
    return RetentionResult(messages, summaries, events, jobs, sends)


def _rows(session: Session, stmt: Any) -> int:
    """Run a bulk UPDATE/DELETE and return how many rows it touched."""
    return cast(CursorResult[Any], session.execute(stmt)).rowcount or 0
