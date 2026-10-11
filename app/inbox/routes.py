"""Staff inbox routes (CLAUDE.md §5). Server-rendered Jinja2 pages, no SPA.

GET  /inbox                      handed-off conversations + upcoming bookings
GET  /inbox/partials/list        the same list, for the 30-second auto-refresh
GET  /inbox/c/{id}               one conversation, reply box, "Return to bot"
GET  /inbox/c/{id}/messages      the messages, for the 30-second auto-refresh
POST /inbox/c/{id}/reply         staff reply (24h window enforced)
POST /inbox/c/{id}/reopen        "Re-open with template" once the window has closed
POST /inbox/c/{id}/release       "Return to bot"
GET  /inbox/login, POST /inbox/login, POST /inbox/logout

Auto-refresh uses a few lines of plain JavaScript polling these partials. The
brief suggests HTMX; the inline script does the same job without loading a
third-party script on a page full of customer conversations.

Staff learn about new handoffs from the WhatsApp/email alerts
(``app/alerts/``); the unread count in the page title covers the rest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.session import get_session
from app.http import ExternalServiceError
from app.inbox import service
from app.inbox.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    LoginThrottle,
    make_session_token,
    password_ok,
    valid_session_token,
)
from app.kb.loader import KnowledgeBase
from app.logging import get_logger
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.window import OutsideServiceWindowError

log = get_logger(__name__)
router = APIRouter(prefix="/inbox")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]

REFRESH_SECONDS = 30
ERRORS = {
    "window": service.WINDOW_CLOSED_TEXT,
    "reopen": "Not sent: the chat is already open, or a re-open message is still unanswered.",
    "send": "WhatsApp didn't accept the message. Please try again in a minute.",
    "empty": "Write a message first.",
    "long": "That message is too long for WhatsApp.",
}


def get_kb(request: Request) -> KnowledgeBase:
    """The knowledge base loaded at startup (``create_app``)."""
    kb: KnowledgeBase = request.app.state.kb
    return kb


def get_wa_client(request: Request) -> WhatsAppClient:
    """The send client created at startup. Tests replace ``app.state.wa_client``."""
    client: WhatsAppClient = request.app.state.wa_client
    return client


def get_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


KbDep = Annotated[KnowledgeBase, Depends(get_kb)]
ClientDep = Annotated[WhatsAppClient, Depends(get_wa_client)]
ThrottleDep = Annotated[LoginThrottle, Depends(get_throttle)]


class NotLoggedInError(Exception):
    """Raised by ``require_login``; ``create_app`` turns it into a redirect to the login page."""


def require_login(request: Request, settings: SettingsDep) -> None:
    """Dependency for every inbox page except login."""
    token = request.cookies.get(COOKIE_NAME)
    if not valid_session_token(settings.inbox_auth_secret.get_secret_value(), token):
        raise NotLoggedInError


LoggedIn = Depends(require_login)


async def _form(request: Request) -> dict[str, str]:
    # Parsed by hand: FastAPI's Form() needs python-multipart, and these forms are tiny.
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ login


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, settings: SettingsDep, throttle: ThrottleDep) -> Response:
    """Check the shared password and set the session cookie."""
    who = request.client.host if request.client else "unknown"
    if throttle.blocked(who):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Wait 15 minutes and try again."},
            status_code=429,
        )
    form = await _form(request)
    secret = settings.inbox_auth_secret.get_secret_value()
    if not password_ok(secret, form.get("password", "")):
        throttle.failed(who)
        log.warning("inbox login failed", extra={"event": "inbox_login_failed"})
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong password."}, status_code=401
        )
    throttle.succeeded(who)
    response = _redirect("/inbox")
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(secret),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/inbox",
    )
    return response


@router.post("/logout")
def logout() -> Response:
    response = _redirect("/inbox/login")
    response.delete_cookie(COOKIE_NAME, path="/inbox")
    return response


# ------------------------------------------------------------------ list


def _list_context(session: Session, settings: Settings, kb: KnowledgeBase) -> dict[str, object]:
    return {
        "handoffs": service.handed_off(session, settings.tz),
        "unread": service.unread_total(session),
        "bookings": service.upcoming_bookings(session, kb, settings.tz),
        "refresh_seconds": REFRESH_SECONDS,
    }


@router.get("", response_class=HTMLResponse, dependencies=[LoggedIn])
def inbox(request: Request, session: SessionDep, settings: SettingsDep, kb: KbDep) -> Response:
    return templates.TemplateResponse(request, "list.html", _list_context(session, settings, kb))


@router.get("/partials/list", response_class=HTMLResponse, dependencies=[LoggedIn])
def inbox_partial(
    request: Request, session: SessionDep, settings: SettingsDep, kb: KbDep
) -> Response:
    ctx = _list_context(session, settings, kb)
    response = templates.TemplateResponse(request, "_list.html", ctx)
    response.headers["X-Unread"] = str(ctx["unread"])
    return response


# ------------------------------------------------------------------ thread


@router.get("/c/{conversation_id}", response_class=HTMLResponse, dependencies=[LoggedIn])
def thread(
    request: Request, conversation_id: int, session: SessionDep, settings: SettingsDep
) -> Response:
    try:
        t = service.open_thread(session, conversation_id, settings.tz)
    except service.NotFoundError:
        return HTMLResponse("Conversation not found", status_code=404)
    error = ERRORS.get(request.query_params.get("error", ""))
    return templates.TemplateResponse(
        request,
        "thread.html",
        {
            "t": t,
            # Reminds staff that the bot stays silent until they hand the chat back.
            "staff_replied": any(m.sender == "staff" for m in t.messages),
            "error": error,
            "window_closed_text": service.WINDOW_CLOSED_TEXT,
            "unread": service.unread_total(session),
            "refresh_seconds": REFRESH_SECONDS,
        },
    )


@router.get("/c/{conversation_id}/messages", response_class=HTMLResponse, dependencies=[LoggedIn])
def thread_messages(
    request: Request, conversation_id: int, session: SessionDep, settings: SettingsDep
) -> Response:
    try:
        t = service.open_thread(session, conversation_id, settings.tz)
    except service.NotFoundError:
        return HTMLResponse("", status_code=404)
    response = templates.TemplateResponse(request, "_messages.html", {"t": t})
    response.headers["X-Unread"] = str(service.unread_total(session))
    response.headers["X-Window-Open"] = "1" if t.window_open else "0"
    return response


@router.post("/c/{conversation_id}/reply", dependencies=[LoggedIn])
async def reply(
    request: Request, conversation_id: int, session: SessionDep, client: ClientDep
) -> Response:
    """Send a staff reply. Errors come back to the thread page as a banner."""
    text = (await _form(request)).get("text", "")
    url = f"/inbox/c/{conversation_id}"
    try:
        service.staff_reply(session, client, conversation_id, text)
    except service.NotFoundError:
        return HTMLResponse("Conversation not found", status_code=404)
    except OutsideServiceWindowError:
        return _redirect(f"{url}?error=window")
    except ExternalServiceError as exc:
        log.error("staff reply failed: %s", exc, extra={"conversation_id": conversation_id})
        return _redirect(f"{url}?error=send")
    except ValueError as exc:
        return _redirect(f"{url}?error={'empty' if 'empty' in str(exc) else 'long'}")
    return _redirect(url)


@router.post("/c/{conversation_id}/reopen", dependencies=[LoggedIn])
def reopen(
    request: Request,
    conversation_id: int,
    session: SessionDep,
    client: ClientDep,
    settings: SettingsDep,
    kb: KbDep,
) -> Response:
    """Send the re-open template (window closed). Errors come back as a banner."""
    url = f"/inbox/c/{conversation_id}"
    try:
        sent = service.reopen(
            session, client, request.app.state.catalog, settings, kb, conversation_id
        )
    except service.NotFoundError:
        return HTMLResponse("Conversation not found", status_code=404)
    except ExternalServiceError as exc:
        log.error("re-open failed: %s", exc, extra={"conversation_id": conversation_id})
        return _redirect(f"{url}?error=send")
    return _redirect(url if sent else f"{url}?error=reopen")


@router.post("/c/{conversation_id}/release", dependencies=[LoggedIn])
def release(conversation_id: int, session: SessionDep) -> Response:
    try:
        service.return_to_bot(session, conversation_id)
    except service.NotFoundError:
        return HTMLResponse("Conversation not found", status_code=404)
    return _redirect("/inbox")
