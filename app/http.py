"""Shared retry/backoff helper for outbound HTTP calls.

CLAUDE.md §9: every external call (Meta, LLM, booking) goes through a thin
client with timeouts, retries with backoff, and structured error logging.
This helper holds the retry policy, so each client stays small.

It's written by hand rather than adding a retry library, per §13 ("ask before
adding a dependency").
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import httpx

from app.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# 429 and 5xx are transient. Other 4xx mean the request itself is wrong, and
# retrying would only repeat the error.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExternalServiceError(Exception):
    """An outbound call failed after all retries, or failed with a non-retryable error."""

    def __init__(
        self,
        service: str,
        message: str,
        status_code: int | None = None,
        *,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status_code = status_code
        # True when the request may have reached the server (e.g. a read timeout),
        # so the send may have happened. Scheduled template sends don't retry these
        # (CLAUDE.md §2: never double-send).
        self.ambiguous = ambiguous


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    service: str,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    retry_ambiguous: bool = True,
) -> httpx.Response:
    """Run ``send`` and retry transient failures with exponential backoff and jitter.

    Args:
        send: Zero-arg callable that performs one HTTP request.
        service: Short name used in logs and errors (e.g. ``"meta"``).
        attempts: Total tries, including the first.
        base_delay: The first backoff in seconds. Doubles after each attempt.
        sleep: Injected for tests.
        retry_ambiguous: Retry transport errors after which the server may have
            acted (read/write timeouts, dropped connections). Pass ``False`` for
            non-idempotent sends that must not be duplicated; connect errors
            (request never sent) are still retried.

    Returns:
        The successful (2xx) response.

    Raises:
        ExternalServiceError: A non-retryable status, or retries ran out.
    """
    last_error: str = "no attempts made"
    last_status: int | None = None
    ambiguous = False
    for attempt in range(1, attempts + 1):
        try:
            response = send()
        except httpx.TransportError as exc:  # timeouts, DNS, connection resets
            last_error, last_status = f"transport error: {type(exc).__name__}", None
            # Connect failures mean the request never left; anything later may have landed.
            ambiguous = not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
            if ambiguous and not retry_ambiguous:
                break
        else:
            ambiguous = False
            if response.is_success:
                return response
            last_status = response.status_code
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in _RETRYABLE_STATUS:
                break
        log.warning(
            "%s call failed (%s)",
            service,
            last_error,
            extra={"attempt": attempt, "status": last_status},
        )
        if attempt < attempts:
            # Jitter only spreads retries out, so it isn't security-sensitive.
            sleep(base_delay * 2 ** (attempt - 1) + random.uniform(0, 0.25))  # noqa: S311
    raise ExternalServiceError(service, last_error, last_status, ambiguous=ambiguous)
