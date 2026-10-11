"""Shared fixtures.

Tests run against in-memory SQLite, so no Postgres is needed locally.
Postgres-only behavior (``SKIP LOCKED`` across replicas) belongs in
integration tests against a real database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Dummy config. It must be set before anything imports app.config.
TEST_ENV = {
    "WHATSAPP_PHONE_NUMBER_ID": "111111111111111",
    "WHATSAPP_BUSINESS_ACCOUNT_ID": "222222222222222",
    "ASSISTANT_NAME": "Sara",
    "BOOKING_PROVIDER": "internal",
    "HANDOFF_HOURS_MESSAGE": "Our team replies Sunday-Thursday, 9am-9pm.",
    "WHATSAPP_ACCESS_TOKEN": "test-access-token",
    "WHATSAPP_APP_SECRET": "test-app-secret",
    "WEBHOOK_VERIFY_TOKEN": "test-verify-token",
    "LLM_API_KEY": "test-llm-key",
    "CALENDAR_CREDENTIALS": "{}",
    "DATABASE_URL": "sqlite://",
    "INBOX_AUTH_SECRET": "test-inbox-secret",
    # The demo clinic is the tests' knowledge base (services, staff, hours, FAQs).
    "BUSINESS_CONFIG_PATH": str(ROOT / "examples" / "demo-clinic" / "business.yaml"),
    "FAQ_PATH": str(ROOT / "examples" / "demo-clinic" / "faqs.yaml"),
    "STYLE_GUIDE_PATH": str(ROOT / "examples" / "demo-clinic" / "style_ar.md"),
    "HANDOFF_CONTACTS": "+966500000900",
    "TEMPLATE_REMINDER": "appointment_reminder",
    "TEMPLATE_BOOKING_UPDATE": "booking_confirmation_update",
    "TEMPLATE_STAFF_ALERT": "staff_handoff_alert",
    "TEMPLATE_REOPEN": "conversation_reopen",
    "TEMPLATE_FOLLOWUP": "lead_followup",
}
os.environ.update(TEST_ENV)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

# Tests must not depend on a developer's local .env (e.g. a QUIET_HOURS override).
Settings.model_config["env_file"] = None
from app.db.models import Base  # noqa: E402
from app.whatsapp.client import WhatsAppClient  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "meta"
APP_SECRET = TEST_ENV["WHATSAPP_APP_SECRET"]


def load_fixture(name: str) -> dict[str, Any]:
    """Load a Meta webhook fixture from ``tests/fixtures/meta``."""
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def freshen(payload: dict[str, Any]) -> dict[str, Any]:
    """Move every message timestamp to now, so the 24h window is open."""
    now = str(int(datetime.now(UTC).timestamp()))
    for entry in payload["entry"]:
        for change in entry["changes"]:
            for msg in change["value"].get("messages", []):
                msg["timestamp"] = now
    return payload


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    """Build an ``X-Hub-Signature-256`` header value the way Meta does."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client: TestClient, payload: dict[str, Any], secret: str = APP_SECRET) -> int:
    """POST a signed payload to ``/webhook`` and return the status code."""
    body = json.dumps(payload).encode()
    r = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"},
    )
    return int(r.status_code)


class FakeMeta:
    """Stands in for the Graph API. Records every request and answers with a fresh wamid."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.OUT{len(self.requests)}"}]})

    @property
    def texts(self) -> list[str]:
        """Bodies of the text messages sent so far."""
        return [r["text"]["body"] for r in self.requests if r.get("type") == "text"]


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def fake_meta() -> FakeMeta:
    return FakeMeta()


@pytest.fixture
def wa_client(settings: Settings, fake_meta: FakeMeta) -> WhatsAppClient:
    return WhatsAppClient(
        settings, http=httpx.Client(transport=httpx.MockTransport(fake_meta.handler))
    )


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], wa_client: WhatsAppClient
) -> Iterator[TestClient]:
    from app import main
    from app.db.session import get_session, session_scope

    app = main.create_app()
    app.state.wa_client = wa_client  # staff replies go to FakeMeta

    def _session() -> Iterator[Session]:
        with session_scope(session_factory) as s:
            yield s

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- days 4-11 fixtures

from app.booking.internal import InternalProvider  # noqa: E402
from app.db.models import Conversation, Customer  # noqa: E402
from app.kb.loader import KnowledgeBase, load_knowledge  # noqa: E402

# Sunday 2026-10-04 09:00 in Riyadh (UTC+3). Monday the 5th is a full working day.
NOW = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)


@pytest.fixture
def kb(settings: Settings) -> KnowledgeBase:
    return load_knowledge(
        settings.business_config_path, settings.faq_path, settings.style_guide_path
    )


@pytest.fixture
def provider(kb: KnowledgeBase, settings: Settings) -> InternalProvider:
    return InternalProvider(kb, settings.tz)


def make_conversation(
    session: Session, wa_id: str = "966500000001", name: str | None = "Aisha"
) -> tuple[Conversation, Customer]:
    """A customer with an open 24h window, flushed so both have ids."""
    customer = Customer(wa_id=wa_id, name=name, language="en")
    session.add(customer)
    session.flush()
    conv = Conversation(customer_id=customer.id, last_customer_msg_at=datetime.now(UTC))
    session.add(conv)
    session.flush()
    return conv, customer
