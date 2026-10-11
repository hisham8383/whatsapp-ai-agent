"""Public routes (no login).

GET /privacy   privacy policy + data deletion instructions, in English and Arabic.
               Meta requires a public privacy policy URL before a WhatsApp app
               can be published (Live mode). Built from the knowledge base and
               config, so it always states this deployment's real retention period.
GET /health    liveness alias. Cloud Run's front end reserves paths ending in "z"
               (``/healthz`` never reaches the app there).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.kb.loader import KnowledgeBase

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Bump when the policy text changes; shown on the page.
POLICY_UPDATED = date(2026, 9, 28)


def _kb(request: Request) -> KnowledgeBase:
    kb: KnowledgeBase = request.app.state.kb
    return kb


@router.get("/privacy", response_class=HTMLResponse)
def privacy(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    kb: Annotated[KnowledgeBase, Depends(_kb)],
) -> Response:
    """The privacy policy page (public, no login)."""
    return templates.TemplateResponse(
        request,
        "privacy.html",
        {
            "b": kb.business,
            "retention_days": settings.log_retention_days,
            "calendar": settings.booking_provider.value == "google_calendar",
            "updated": POLICY_UPDATED,
        },
    )


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe that works on Cloud Run (see module docstring)."""
    return {"status": "ok"}
