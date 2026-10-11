"""Thin client for sending messages through the WhatsApp Cloud API.

Every outbound message goes through here, whether the bot or staff wrote it
(from the inbox). The client:
    * enforces the 24h customer-service window on every free-form send
      (``window.py``). ``send_template`` is the one path that skips it, because
      approved templates are exactly what WhatsApp allows outside the window
      (CLAUDE.md §4);
    * applies timeouts and retries (``app/http.py``),
    * returns Meta's message id, so status webhooks can update the stored row.

It does not touch the database. The caller stores the outbound ``Message``
with the returned id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.http import DEFAULT_TIMEOUT, ExternalServiceError, request_with_retry
from app.whatsapp.window import ensure_window_open

# Meta limits for interactive list messages.
MAX_LIST_ROWS = 10
MAX_ROW_TITLE = 24
MAX_ROW_DESCRIPTION = 72
MAX_LIST_BUTTON = 20
# Meta limits for reply-button messages.
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20


@dataclass(frozen=True, slots=True)
class ListRow:
    """One selectable option in an interactive list, e.g. a time slot."""

    id: str
    title: str
    description: str | None = None


class WhatsAppClient:
    """Sends text and interactive-list messages, always inside the 24h window."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        """
        Args:
            settings: Supplies the phone number id, API version, and access token.
            http: Injected in tests (``httpx.MockTransport``). Defaults to a
                client with ``DEFAULT_TIMEOUT``.
        """
        self._url = f"{settings.graph_api_base_url}/{settings.whatsapp_phone_number_id}/messages"
        self._http = http or httpx.Client(timeout=DEFAULT_TIMEOUT)
        self._token = settings.whatsapp_access_token

    def _post(self, body: dict[str, Any], *, retry_ambiguous: bool = True) -> str:
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        response = request_with_retry(
            lambda: self._http.post(self._url, json=body, headers=headers),
            service="meta",
            retry_ambiguous=retry_ambiguous,
        )
        try:
            return str(response.json()["messages"][0]["id"])
        except (KeyError, IndexError, ValueError) as exc:
            raise ExternalServiceError("meta", "unexpected send response shape") from exc

    def send_text(self, to: str, text: str, *, last_customer_msg_at: datetime | None) -> str:
        """Send a text message.

        Args:
            to: Recipient WhatsApp id.
            text: Message body. Keep it short, since this is WhatsApp.
            last_customer_msg_at: ``conversations.last_customer_msg_at``. It's a
                required keyword, so the guard can't be skipped by accident.

        Returns:
            Meta's ``wamid`` for the sent message.

        Raises:
            OutsideServiceWindowError: The window is closed (§4). Nothing is sent.
            ExternalServiceError: Meta rejected the send or was unreachable.
        """
        ensure_window_open(last_customer_msg_at)
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text, "preview_url": False},
            }
        )

    def send_buttons(
        self,
        to: str,
        body: str,
        buttons: list[ListRow],
        *,
        last_customer_msg_at: datetime | None,
    ) -> str:
        """Send up to 3 reply buttons, used for "Confirm / Change" before a booking change (§5).

        The tapped button's ``id`` comes back as ``reply_id``. Titles are cut to
        20 characters (Meta's limit).

        Args:
            to: Recipient WhatsApp id.
            body: Text shown above the buttons.
            buttons: Options; only ``id`` and ``title`` are used.
            last_customer_msg_at: See ``send_text``.

        Returns:
            Meta's ``wamid`` for the sent message.

        Raises:
            OutsideServiceWindowError: The window is closed. Nothing is sent.
            ExternalServiceError: Meta rejected the send or was unreachable.
        """
        ensure_window_open(last_customer_msg_at)
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {"id": b.id, "title": b.title[:MAX_BUTTON_TITLE]},
                            }
                            for b in buttons[:MAX_BUTTONS]
                        ]
                    },
                },
            }
        )

    def send_list(
        self,
        to: str,
        body: str,
        button: str,
        rows: list[ListRow],
        *,
        last_customer_msg_at: datetime | None,
    ) -> str:
        """Send an interactive list, which is used for time-slot selection (§4).

        Meta allows at most 10 rows, row titles up to 24 characters, and a
        button label up to 20. Extra rows are dropped and long strings are
        truncated, so a long slot list never makes the send fail.

        Args:
            to: Recipient WhatsApp id.
            body: Text shown above the list button.
            button: Label of the button that opens the list.
            rows: Options. The chosen row's ``id`` comes back as ``reply_id``.
            last_customer_msg_at: See ``send_text``.

        Returns:
            Meta's ``wamid`` for the sent message.

        Raises:
            OutsideServiceWindowError: The window is closed. Nothing is sent.
            ExternalServiceError: Meta rejected the send or was unreachable.
        """
        ensure_window_open(last_customer_msg_at)
        section_rows = [
            {
                "id": r.id,
                "title": r.title[:MAX_ROW_TITLE],
                **({"description": r.description[:MAX_ROW_DESCRIPTION]} if r.description else {}),
            }
            for r in rows[:MAX_LIST_ROWS]
        ]
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body},
                    "action": {
                        "button": button[:MAX_LIST_BUTTON],
                        "sections": [{"rows": section_rows}],
                    },
                },
            }
        )

    def send_template(
        self,
        to: str,
        name: str,
        language: str,
        params: list[str],
        *,
        quick_reply_payloads: list[str] | None = None,
    ) -> str:
        """Send an approved template. Allowed outside the 24h window (§4).

        Used for reminders, lead follow-ups, staff alerts, conversation re-open
        and booking updates. Names come from ``TEMPLATE_*`` config, never from
        literals, so a renamed template is a config change (§4).

        Args:
            to: Recipient WhatsApp id (digits only).
            name: Approved template name.
            language: ``"ar"`` or ``"en"``: the template's language code.
            params: Body parameters ``{{1}}..{{n}}``, in order.
            quick_reply_payloads: One payload per quick-reply button, in order.
                It comes back as ``reply_id`` when the button is tapped, so the
                reply can be routed (e.g. ``"rem:42:reschedule"``).

        Returns:
            Meta's ``wamid``.

        Raises:
            ExternalServiceError: Meta rejected the send or was unreachable.
                ``ambiguous`` is set when the send may have gone through anyway;
                ambiguous failures are not retried here, so a reminder is never
                sent twice (§2).
        """
        components: list[dict[str, Any]] = []
        if params:
            components.append(
                {
                    "type": "body",
                    # Meta rejects empty text parameters (error 131008).
                    "parameters": [{"type": "text", "text": p or "-"} for p in params],
                }
            )
        for index, payload in enumerate(quick_reply_payloads or []):
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": str(index),
                    "parameters": [{"type": "payload", "payload": payload}],
                }
            )
        return self._post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": language},
                    **({"components": components} if components else {}),
                },
            },
            retry_ambiguous=False,
        )
