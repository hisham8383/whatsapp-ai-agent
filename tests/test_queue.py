"""Job queue semantics and the worker loop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Job, JobStatus
from app.worker import queue
from app.worker.runner import Worker


def test_claim_complete(session: Session) -> None:
    queue.enqueue(session, "t", {"x": 1})
    job = queue.claim_next(session)
    assert job is not None and job.status is JobStatus.RUNNING and job.attempts == 1
    assert queue.claim_next(session) is None  # already claimed
    queue.complete(session, job)
    assert job.status.value == JobStatus.DONE.value  # .value: mypy keeps the RUNNING narrowing


def test_future_job_not_claimed(session: Session) -> None:
    queue.enqueue(session, "t", {}, run_at=datetime.now(UTC) + timedelta(minutes=5))
    assert queue.claim_next(session) is None


def test_filter_by_type(session: Session) -> None:
    queue.enqueue(session, "other", {})
    assert queue.claim_next(session, ["t"]) is None
    assert queue.claim_next(session, ["other"]) is not None


def test_stale_lock_reclaimed(session: Session) -> None:
    job = queue.enqueue(session, "t", {})
    assert queue.claim_next(session) is job
    job.locked_at = datetime.now(UTC) - queue.STALE_LOCK_AFTER - timedelta(seconds=1)
    assert queue.claim_next(session) is job
    assert job.attempts == 2


def test_fail_backs_off_then_gives_up(session: Session) -> None:
    job = queue.enqueue(session, "t", {})
    for _ in range(queue.MAX_ATTEMPTS):
        job.run_at = datetime(2000, 1, 1, tzinfo=UTC)  # make it due now
        assert queue.claim_next(session) is job
        queue.fail(session, job, "boom")
    assert job.status is JobStatus.FAILED
    assert job.last_error == "boom"


def test_worker_empty_queue(session_factory: sessionmaker[Session]) -> None:
    assert not Worker({"t": lambda _s, _p: None}).run_once(session_factory)


def test_worker_dispatches_and_completes(session_factory: sessionmaker[Session]) -> None:
    seen: list[dict[str, Any]] = []
    with session_factory() as s:
        queue.enqueue(s, "t", {"x": 1})
        s.commit()
    assert Worker({"t": lambda _s, p: seen.append(p)}).run_once(session_factory)
    assert seen == [{"x": 1}]
    with session_factory() as s:
        assert s.execute(select(Job.status)).scalar_one() is JobStatus.DONE


def test_worker_failure_rolls_back_and_reschedules(
    session_factory: sessionmaker[Session],
) -> None:
    def boom(session: Session, _payload: dict[str, Any]) -> None:
        queue.enqueue(session, "side-effect", {})  # must be rolled back
        raise RuntimeError("provider down")

    with session_factory() as s:
        queue.enqueue(s, "t", {})
        s.commit()
    assert Worker({"t": boom}).run_once(session_factory)
    with session_factory() as s:
        [job] = s.execute(select(Job)).scalars().all()
        assert job.status is JobStatus.QUEUED
        assert job.last_error == "RuntimeError: provider down"
