"""Wake a scale-to-zero worker after new work is queued.

Default deployments run the worker always on, and this module does nothing.
For low-traffic or demo hosting, the worker can run with zero idle instances
(Cloud Run: ``min-instances 0`` plus CPU always allocated while an instance
exists). A request to the worker's URL makes the platform start an instance.
The worker's health server answers the request (``app/worker/health.py``),
and its job loop drains the queue. The instance shuts down on its own after
a quiet period.

The ping is best-effort: the job is already committed in Postgres, so a
failed ping only delays the reply until the next message wakes the worker.
It never raises, and it waits at most ``TIMEOUT`` seconds, so the webhook
still answers Meta quickly (CLAUDE.md §4). The platform starts the instance
as soon as the request arrives, even if the client gave up waiting.
"""

from __future__ import annotations

import httpx

from app.logging import get_logger

log = get_logger(__name__)

TIMEOUT = httpx.Timeout(1.0, connect=1.0)


def wake_worker(url: str, http: httpx.Client | None = None) -> None:
    """Send a best-effort GET to ``url`` so a scaled-to-zero worker starts.

    Args:
        url: ``WORKER_WAKE_URL``. Empty means "worker is always on": nothing is sent.
        http: Injected in tests.
    """
    if not url:
        return
    try:
        client = http or httpx.Client(timeout=TIMEOUT)
        client.get(url)
    except httpx.HTTPError as exc:
        # Timeouts are expected while the worker cold-starts; the start is already triggered.
        log.info("worker wake ping did not complete: %s", type(exc).__name__)
