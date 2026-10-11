"""Waking a scale-to-zero worker: pinged only when a message was stored and a URL is set."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import Job
from app.worker.wake import wake_worker
from tests.conftest import freshen, load_fixture, post_webhook


def test_wake_is_best_effort() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("cold start")

    wake_worker("https://worker.example/", http=httpx.Client(transport=httpx.MockTransport(boom)))
    wake_worker("")  # no URL: nothing happens


@pytest.fixture
def pings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr("app.webhook.routes.wake_worker", lambda url: seen.append(url))
    return seen


def test_new_message_wakes_worker_after_commit(
    client: TestClient,
    session_factory: sessionmaker[Session],
    pings: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "worker_wake_url", "https://worker.example/")
    assert post_webhook(client, freshen(load_fixture("text.json"))) == 200
    assert pings == ["https://worker.example/"]
    with session_factory() as s:
        assert s.execute(select(Job)).scalars().all()  # committed before the ping


def test_no_ping_without_url_or_new_message(
    client: TestClient, pings: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert post_webhook(client, freshen(load_fixture("text.json"))) == 200  # URL unset
    monkeypatch.setattr(get_settings(), "worker_wake_url", "https://worker.example/")
    assert post_webhook(client, load_fixture("status_failed.json")) == 200  # status only
    assert pings == []
