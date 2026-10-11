"""A tiny HTTP health endpoint for the worker process.

The worker has no HTTP API; it only polls the job queue. Some platforms still
require every container to listen on a port: Cloud Run services check that
``$PORT`` answers before sending the instance any traffic, and Kubernetes
liveness probes can use it too. When ``PORT`` is set, ``python -m app worker``
starts this server in a background thread; it answers ``200 ok`` to any GET
and never touches the database or the job loop.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # name required by BaseHTTPRequestHandler
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request logging; probes would flood the logs."""


def start_health_server(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:  # noqa: S104
    """Serve ``200 ok`` on ``port`` from a daemon thread and return the server.

    Args:
        port: Port to listen on (``$PORT`` on Cloud Run). ``0`` picks a free one (tests).
        host: Interface; all interfaces by default, as containers require.
    """
    server = ThreadingHTTPServer((host, port), _Ok)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server
