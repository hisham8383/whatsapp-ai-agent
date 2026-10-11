"""The worker's health endpoint answers 200 without touching anything else."""

from __future__ import annotations

import httpx

from app.worker.health import start_health_server


def test_health_server_answers_ok() -> None:
    server = start_health_server(0, host="127.0.0.1")
    try:
        port = server.server_address[1]
        r = httpx.get(f"http://127.0.0.1:{port}/anything", timeout=5)
        assert (r.status_code, r.text) == (200, "ok")
    finally:
        server.shutdown()
