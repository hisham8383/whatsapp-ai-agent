"""Webhook authenticity checks (CLAUDE.md §4).

Two separate mechanisms from Meta:

1. **Subscription handshake** (``GET /webhook``): when the webhook URL is
   registered, Meta sends ``hub.mode=subscribe``, ``hub.verify_token`` and
   ``hub.challenge``. We echo the challenge only if the token matches ours.
2. **Payload signature** (``POST /webhook``): Meta signs every delivery with
   HMAC-SHA256 of the *raw request body*, keyed by the app secret, and sends it
   as ``X-Hub-Signature-256: sha256=<hex>``. Anything that fails the check is
   rejected, because otherwise anyone who learns the URL could inject fake
   customer messages.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_PREFIX = "sha256="


def verify_subscription(mode: str | None, token: str | None, expected_token: str) -> bool:
    """Return True if a ``GET /webhook`` handshake is valid.

    Args:
        mode: The ``hub.mode`` query param. It must be ``"subscribe"``.
        token: The ``hub.verify_token`` query param.
        expected_token: Our ``WEBHOOK_VERIFY_TOKEN``.
    """
    if mode != "subscribe" or token is None:
        return False
    return hmac.compare_digest(token.encode(), expected_token.encode())


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Return True if ``X-Hub-Signature-256`` matches the raw body.

    The raw bytes are used, never re-serialized JSON, because re-serializing
    changes whitespace and key order and breaks the HMAC. The comparison is
    constant-time so the signature can't be learned through timing.

    Args:
        raw_body: Exact bytes received.
        header: The ``X-Hub-Signature-256`` header value, or ``None`` if absent.
        app_secret: ``WHATSAPP_APP_SECRET``.
    """
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len(SIGNATURE_PREFIX) :], expected)
