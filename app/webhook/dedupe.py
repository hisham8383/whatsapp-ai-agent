"""Idempotency for inbound WhatsApp messages (CLAUDE.md §4).

Meta delivers webhooks at least once. It retries on timeouts and non-2xx
responses, and occasionally sends the same message twice anyway. Without a
guard, a customer would get two replies, or worse, two bookings.

The guard is the ``processed_events`` table, whose primary key is the WhatsApp
message id. Claiming an id is a plain INSERT, and the database's unique
constraint decides the race: if two API replicas receive the same delivery at
the same moment, exactly one INSERT succeeds. No read-then-write gap exists.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import ProcessedEvent


def claim_event(session: Session, wa_message_id: str) -> bool:
    """Record ``wa_message_id`` as processed and report whether it is new.

    The INSERT runs inside a SAVEPOINT, so a duplicate rolls back only itself.
    Other messages in the same webhook batch are still stored.

    Args:
        session: Open session. The caller commits, which makes the claim durable
            together with the stored message and its job.
        wa_message_id: Meta's ``wamid``.

    Returns:
        True the first time the id is seen, False for a duplicate delivery.
    """
    try:
        with session.begin_nested():
            session.add(ProcessedEvent(wa_message_id=wa_message_id))
            session.flush()
    except IntegrityError:
        return False
    return True
