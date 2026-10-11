"""Idempotency ledger (CLAUDE.md §4: skip duplicate message ids)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ProcessedEvent
from app.webhook.dedupe import claim_event


def test_first_claim_wins(session: Session) -> None:
    assert claim_event(session, "wamid.A")
    assert not claim_event(session, "wamid.A")
    assert claim_event(session, "wamid.B")


def test_duplicate_does_not_abort_transaction(session: Session) -> None:
    claim_event(session, "wamid.A")
    claim_event(session, "wamid.A")  # rolls back only its own SAVEPOINT
    session.commit()
    assert session.scalar(select(func.count()).select_from(ProcessedEvent)) == 1


def test_claim_survives_across_sessions(session: Session) -> None:
    claim_event(session, "wamid.A")
    session.commit()
    session.close()
    assert not claim_event(session, "wamid.A")
