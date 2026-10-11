"""Database engine and session factory.

One engine per process. Sessions are short-lived: a webhook request or one
job each gets its own session, which commits or rolls back as a unit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create the process-wide engine from ``DATABASE_URL``."""
    url = get_settings().database_url.get_secret_value()
    return create_engine(url, pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Return the session factory bound to the process engine."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on any exception.

    Args:
        factory: Optional session factory. Tests pass one bound to SQLite.
    """
    session = (factory or get_sessionmaker())()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: a request-scoped session that commits if the handler returns normally.

    Tests override this dependency to point at SQLite.
    """
    with session_scope() as session:
        yield session
