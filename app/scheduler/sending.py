"""Exactly-once claim-and-send loop shared by reminders, follow-ups, and staff alerts.

CLAUDE.md §2 "never double-send", §3: the scheduler polls due rows with
``SELECT ... FOR UPDATE SKIP LOCKED`` and marks each row sent **in the same
transaction** that records the WhatsApp message id.

How one row is processed (``drain``):

1. Open a transaction and lock the oldest due ``pending`` row. A second
   scheduler (a mistaken extra replica, or a new pod starting while the old one
   drains) skips locked rows, so it can never pick the same row.
2. Send, then set ``status=sent`` + ``wa_message_id`` and commit. The lock is
   held during the send, so the row can't be claimed again meanwhile.
3. On failure: a definite failure is retried once after ``RETRY_DELAY`` (§4:
   "retried once"); an *ambiguous* one (a timeout after the request left, so
   Meta may have sent it) is marked failed and never retried.

Anything after a successful send that could fail (storing the inbox copy)
runs through ``after_send`` inside a savepoint, so it can't undo the "sent"
mark. The one gap left is the process dying between Meta accepting the send
and the commit: milliseconds wide, and closing it would need Meta-side
idempotency keys, which the Cloud API doesn't offer.

SQLite (unit tests) ignores ``FOR UPDATE``; the two-runner guarantee is
checked against real Postgres in ``scripts/smoke_postgres.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import FollowUp, Reminder, SendStatus, StaffAlert
from app.db.session import session_scope
from app.http import ExternalServiceError
from app.logging import get_logger

log = get_logger(__name__)

ScheduledSend = Reminder | FollowUp | StaffAlert
SEND_MODELS: tuple[type[ScheduledSend], ...] = (
    Reminder,
    FollowUp,
    StaffAlert,
)

MAX_ATTEMPTS = 2  # the first send plus one retry (§4)
RETRY_DELAY = timedelta(minutes=5)
BATCH_LIMIT = 200  # rows per model per tick, so one tick can't run forever

Handler = Callable[[Session, ScheduledSend, datetime], None]


def claim_one[T: ScheduledSend](session: Session, model: type[T], now: datetime) -> T | None:
    """Lock and return the oldest due pending row of ``model``, or ``None``.

    Rows locked by another transaction are skipped, not waited for.
    """
    stmt = (
        select(model)
        .where(model.status == SendStatus.PENDING, model.send_at <= now)
        .order_by(model.send_at, model.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return session.execute(stmt).scalar_one_or_none()


def mark_sent(row: ScheduledSend, wa_message_id: str | None, now: datetime) -> None:
    """Record a successful send. ``wa_message_id`` is ``None`` for email alerts."""
    row.status = SendStatus.SENT
    row.wa_message_id = wa_message_id
    row.sent_at = now
    row.attempts += 1
    row.error_code = None


def mark_skipped(row: ScheduledSend) -> None:
    """A rule said no at send time (slot passed, opted out, booking cancelled)."""
    row.status = SendStatus.SKIPPED


def mark_attempt_failed(
    row: ScheduledSend, error_code: str, now: datetime, *, retry: bool = True
) -> None:
    """Count a failed attempt: reschedule the one retry, or give up.

    Args:
        row: The row being sent.
        error_code: Meta's error code or a short reason, stored for the inbox.
        now: Current time.
        retry: ``False`` for ambiguous failures, which must not be retried.
    """
    row.attempts += 1
    row.error_code = error_code[:20]
    if retry and row.attempts < MAX_ATTEMPTS:
        row.send_at = now + RETRY_DELAY
    else:
        row.status = SendStatus.FAILED


def handle_send_error(row: ScheduledSend, exc: ExternalServiceError, now: datetime) -> None:
    """``mark_attempt_failed`` for an exception from ``WhatsAppClient`` or the mail client."""
    code = str(exc.status_code) if exc.status_code else ("ambiguous" if exc.ambiguous else "error")
    mark_attempt_failed(row, code, now, retry=not exc.ambiguous)
    log.warning(
        "%s send failed: %s",
        type(row).__tablename__,
        exc,
        extra={"event": "send_failed", "error_code": code, "attempt": row.attempts},
    )


def drain(
    model: type[ScheduledSend],
    handler: Handler,
    clock: Callable[[], datetime],
    factory: sessionmaker[Session] | None = None,
    limit: int = BATCH_LIMIT,
) -> int:
    """Send every due row of ``model``, one transaction per row. Returns rows processed.

    Args:
        model: ``Reminder``, ``FollowUp`` or ``StaffAlert``.
        handler: Sends one locked row and sets its status (``mark_sent``,
            ``mark_skipped`` or ``handle_send_error``). Runs inside the row's
            transaction.
        clock: Returns "now" (UTC).
        factory: Session factory; tests pass one bound to SQLite.
        limit: Maximum rows this call processes.

    Failure modes:
        An unexpected exception in ``handler`` is contained in a SAVEPOINT: its
        partial writes roll back, but the row stays locked and is marked
        ``failed`` ("crash") in the same transaction. It is never retried,
        because the crash may have come after Meta accepted the send. (Found
        by the two-runner smoke test: releasing the row first let the other
        scheduler resend it.)
    """
    done = 0
    for _ in range(limit):
        with session_scope(factory) as session:
            now = clock()
            row = claim_one(session, model, now)
            if row is None:
                break
            process_row(session, row, handler, now)
        done += 1
    return done


def process_row(session: Session, row: ScheduledSend, handler: Handler, now: datetime) -> None:
    """Run ``handler`` on one locked row, containing crashes (see ``drain``).

    Also used by the worker's immediate staff-alert job, so both paths share
    the same no-retry-after-crash rule.
    """
    try:
        with session.begin_nested():
            handler(session, row, now)
    except Exception:
        log.exception("%s handler crashed", type(row).__tablename__, extra={"event": "send_crash"})
        session.refresh(row)  # the savepoint rolled back the handler's changes
        mark_attempt_failed(row, "crash", now, retry=False)


def after_send(session: Session, what: str, record: Callable[[], object]) -> None:
    """Run bookkeeping that follows a successful send, without risking the send's record.

    A failure here (say, storing the inbox copy) is logged and rolled back to
    a savepoint, so the row still commits as ``sent`` and is never resent.
    """
    try:
        with session.begin_nested():
            record()
    except Exception:
        log.exception("after-send bookkeeping failed: %s", what, extra={"event": "after_send"})


def apply_failed_status(
    session: Session, wa_message_id: str, error_code: str | None, now: datetime
) -> bool:
    """A status webhook reported a failed delivery: retry once, then give up (§4).

    Meta can accept a template send and fail it later (e.g. the recipient
    isn't on WhatsApp, or a MARKETING template was throttled). The message was
    definitely not delivered, so a retry can't double-send.

    Returns:
        True if the id belonged to a reminder, follow-up, or staff alert.
    """
    for model in SEND_MODELS:
        row = cast(
            ScheduledSend | None,
            session.execute(
                select(model).where(model.wa_message_id == wa_message_id)
            ).scalar_one_or_none(),
        )
        if row is None:
            continue
        if row.status is not SendStatus.SENT:
            return True  # already handled (duplicate status webhook)
        row.error_code = (error_code or "failed")[:20]
        row.wa_message_id = None  # free the unique id; a retry gets a new one
        # ``attempts`` already counts the accepted send.
        if row.attempts < MAX_ATTEMPTS:
            row.status = SendStatus.PENDING
            row.send_at = now + RETRY_DELAY
        else:
            row.status = SendStatus.FAILED
        log.warning(
            "%s delivery failed",
            model.__tablename__,
            extra={"event": "delivery_failed", "error_code": row.error_code},
        )
        return True
    return False
