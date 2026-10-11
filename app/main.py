"""FastAPI app: wires routes together. All logic lives in services (CLAUDE.md §9).

Routes:
    GET  /healthz   liveness probe
    GET  /webhook   Meta subscription handshake     (``app/webhook/routes.py``)
    POST /webhook   Meta event delivery              (``app/webhook/routes.py``)
    /inbox/*        staff inbox                      (``app/inbox/routes.py``)
    GET  /privacy   public privacy policy            (``app/public/routes.py``)
    GET  /health    liveness alias for Cloud Run     (``app/public/routes.py``)
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

from app.config import get_settings
from app.inbox.auth import LoginThrottle
from app.inbox.routes import NotLoggedInError
from app.inbox.routes import router as inbox_router
from app.kb.loader import load_knowledge
from app.logging import configure_logging
from app.public.routes import router as public_router
from app.webhook.routes import router as webhook_router
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.templates import load_catalog


def create_app() -> FastAPI:
    """Build the app. Settings and knowledge are validated here, so bad config fails at boot.

    Raises:
        pydantic.ValidationError: A required setting is missing (§8).
        app.kb.loader.KnowledgeError: A knowledge file is invalid (§5).
    """
    configure_logging()
    settings = get_settings()  # refuse to start if a required key is missing (§8)
    app = FastAPI(title="WhatsApp Booking Agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.kb = load_knowledge(
        settings.business_config_path, settings.faq_path, settings.style_guide_path
    )
    app.state.catalog = load_catalog(settings)  # also validates TEMPLATE_* names (§8)
    app.state.wa_client = WhatsAppClient(settings)
    app.state.login_throttle = LoginThrottle()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(NotLoggedInError)
    def _to_login(request: Request, exc: NotLoggedInError) -> Response:
        return RedirectResponse("/inbox/login", status_code=303)

    app.include_router(webhook_router)
    app.include_router(inbox_router)
    app.include_router(public_router)
    return app
