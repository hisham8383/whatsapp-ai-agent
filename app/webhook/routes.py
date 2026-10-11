"""Webhook HTTP routes. They check authenticity, then hand off to the service.

GET  /webhook   Meta subscription handshake
POST /webhook   Meta event delivery: verify signature → parse → ingest → 200

No business logic lives here (CLAUDE.md §9).
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_session
from app.logging import get_logger
from app.webhook.parse import parse_webhook
from app.webhook.service import ingest
from app.webhook.verify import verify_signature, verify_subscription
from app.worker.wake import wake_worker

log = get_logger(__name__)
router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/webhook", response_class=PlainTextResponse)
def webhook_verify(
    settings: SettingsDep,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str, Query(alias="hub.challenge")] = "",
) -> str:
    """Echo ``hub.challenge`` if the verify token matches, else 403."""
    if not verify_subscription(mode, token, settings.webhook_verify_token.get_secret_value()):
        raise HTTPException(status_code=403, detail="verification failed")
    return challenge


@router.post("/webhook")
async def webhook_receive(request: Request, settings: SettingsDep, session: SessionDep) -> Response:
    """Accept a Meta delivery. Anything unsigned or wrongly signed is rejected with 401."""
    # The signature covers the exact raw bytes, so read them before any parsing (§4).
    raw = await request.body()
    if not verify_signature(
        raw,
        request.headers.get("X-Hub-Signature-256"),
        settings.whatsapp_app_secret.get_secret_value(),
    ):
        log.warning("webhook signature mismatch", extra={"event": "bad_signature"})
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    result = ingest(session, parse_webhook(payload))
    log.info(
        "webhook ingested: %d stored, %d duplicate, %d statuses",
        result.stored,
        result.duplicates,
        result.statuses_applied,
    )
    if result.stored and settings.worker_wake_url:
        # Commit first, so the worker finds the job however fast it starts.
        session.commit()
        await run_in_threadpool(wake_worker, settings.worker_wake_url)
    # 200 for every signed payload (duplicates and ignored types too), so Meta
    # stops retrying. The worker does the rest asynchronously.
    return Response(status_code=200)
