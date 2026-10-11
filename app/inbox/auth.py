"""Inbox authentication: one shared password, no user management (CLAUDE.md §5).

The password is ``INBOX_AUTH_SECRET``. The client reads it from their own
secret store; it's never sent in chat (secrets never travel over chat). After login the
browser gets a signed, expiring session cookie:

    <expiry unix seconds>.<HMAC-SHA256(INBOX_AUTH_SECRET, "inbox:<expiry>")>

Nothing is stored server-side, so this works across API replicas. Rotating
``INBOX_AUTH_SECRET`` logs everyone out. The cookie is HttpOnly and
SameSite=Strict, so other sites can't use it to post replies (CSRF), and
Secure whenever the request came in over HTTPS.

Repeated wrong passwords from one address are throttled in memory (per API
process), which slows guessing without adding a dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

COOKIE_NAME = "inbox_session"
SESSION_SECONDS = 12 * 3600  # one working day
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60


def _sign(secret: str, expiry: int) -> str:
    return hmac.new(secret.encode(), f"inbox:{expiry}".encode(), hashlib.sha256).hexdigest()


def make_session_token(secret: str, now: float | None = None) -> str:
    """Return a cookie value valid for ``SESSION_SECONDS``."""
    expiry = int((now or time.time()) + SESSION_SECONDS)
    return f"{expiry}.{_sign(secret, expiry)}"


def valid_session_token(secret: str, token: str | None, now: float | None = None) -> bool:
    """True if ``token`` was signed with ``secret`` and hasn't expired."""
    if not token or "." not in token:
        return False
    raw_expiry, sig = token.split(".", 1)
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return False
    if expiry < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(secret, expiry))


def password_ok(secret: str, given: str) -> bool:
    """Constant-time comparison, so response time doesn't leak the password."""
    return hmac.compare_digest(given.encode(), secret.encode())


class LoginThrottle:
    """Counts recent failed logins per client address."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] < now - FAILURE_WINDOW_SECONDS:
            q.popleft()
        return q

    def blocked(self, key: str, now: float | None = None) -> bool:
        """True after ``MAX_FAILURES`` failures within ``FAILURE_WINDOW_SECONDS``."""
        return len(self._recent(key, now or time.time())) >= MAX_FAILURES

    def failed(self, key: str, now: float | None = None) -> None:
        now = now or time.time()
        self._recent(key, now).append(now)

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)
