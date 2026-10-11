"""Turn Meta webhook payloads into typed events.

A single delivery can batch several ``entry[].changes[]``, and each change can
carry ``messages`` (from customers), ``statuses`` (delivery receipts for our
sends), or both. This module flattens all of that into a list of
``InboundMessage`` and ``StatusUpdate`` objects, so the service layer never
touches raw dict shapes.

Message types (CLAUDE.md §4):
    * ``text``: plain text
    * ``interactive``: a button or list reply (e.g. a time slot picked from
      the list we sent). We keep the reply id and title.
    * anything else (audio, image, sticker, video, document, location, ...)
      becomes ``type="unsupported"``, and the worker replies asking for text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# "button" is a tap on a template's quick reply, e.g. "Reschedule" on a
# reminder (§4); its payload comes back as ``reply_id``.
SUPPORTED_TYPES = {"text", "interactive", "button"}


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One customer message."""

    wa_message_id: str
    wa_id: str  # customer's WhatsApp number (E.164 without "+")
    profile_name: str | None
    type: str  # "text" | "interactive" | "button" | "unsupported"
    text: str | None  # message text, or the chosen option's title
    reply_id: str | None  # id of the chosen interactive option, if any
    original_type: str  # the type Meta sent, e.g. "audio"
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class StatusUpdate:
    """A delivery receipt for a message we sent."""

    wa_message_id: str
    status: str  # sent | delivered | read | failed
    recipient_id: str
    error_code: str | None
    error_title: str | None
    timestamp: datetime


@dataclass(slots=True)
class ParsedWebhook:
    messages: list[InboundMessage]
    statuses: list[StatusUpdate]


def _ts(raw: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _parse_message(msg: dict[str, Any], names: dict[str, str]) -> InboundMessage | None:
    msg_id, sender, mtype = msg.get("id"), msg.get("from"), msg.get("type", "")
    if not msg_id or not sender:
        return None
    text: str | None = None
    reply_id: str | None = None
    if mtype == "text":
        text = (msg.get("text") or {}).get("body")
    elif mtype == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        text, reply_id = reply.get("title"), reply.get("id")
    elif mtype == "button":
        button = msg.get("button") or {}
        text, reply_id = button.get("text"), button.get("payload")
    return InboundMessage(
        wa_message_id=msg_id,
        wa_id=sender,
        profile_name=names.get(sender),
        type=mtype if mtype in SUPPORTED_TYPES else "unsupported",
        text=text,
        reply_id=reply_id,
        original_type=mtype,
        timestamp=_ts(msg.get("timestamp")),
    )


def _parse_status(st: dict[str, Any]) -> StatusUpdate | None:
    if not st.get("id") or not st.get("status"):
        return None
    errors = st.get("errors") or [{}]
    return StatusUpdate(
        wa_message_id=st["id"],
        status=st["status"],
        recipient_id=st.get("recipient_id", ""),
        error_code=str(errors[0]["code"]) if errors[0].get("code") is not None else None,
        error_title=errors[0].get("title"),
        timestamp=_ts(st.get("timestamp")),
    )


def parse_webhook(payload: dict[str, Any]) -> ParsedWebhook:
    """Flatten a WhatsApp Business webhook body into typed events.

    Unknown or malformed pieces are skipped rather than raising an error. A 4xx
    would make Meta retry a payload we will never be able to parse.
    """
    result = ParsedWebhook(messages=[], statuses=[])
    if payload.get("object") != "whatsapp_business_account":
        return result
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                c.get("wa_id", ""): (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts") or []
            }
            for msg in value.get("messages") or []:
                if parsed := _parse_message(msg, names):
                    result.messages.append(parsed)
            for st in value.get("statuses") or []:
                if parsed_status := _parse_status(st):
                    result.statuses.append(parsed_status)
    return result
