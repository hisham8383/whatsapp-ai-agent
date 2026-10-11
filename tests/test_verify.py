"""Webhook handshake and signature checks (CLAUDE.md §4)."""

from __future__ import annotations

from app.webhook.verify import verify_signature, verify_subscription
from tests.conftest import sign

SECRET = "test-app-secret"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def test_subscription_ok() -> None:
    assert verify_subscription("subscribe", "tok", "tok")


def test_subscription_wrong_token() -> None:
    assert not verify_subscription("subscribe", "nope", "tok")


def test_subscription_wrong_mode() -> None:
    assert not verify_subscription("unsubscribe", "tok", "tok")


def test_subscription_missing_token() -> None:
    assert not verify_subscription("subscribe", None, "tok")


def test_signature_valid() -> None:
    assert verify_signature(BODY, sign(BODY, SECRET), SECRET)


def test_signature_tampered_body() -> None:
    assert not verify_signature(BODY + b" ", sign(BODY, SECRET), SECRET)


def test_signature_reserialized_json_fails() -> None:
    # Same JSON, different whitespace: the HMAC must be over the raw bytes.
    assert not verify_signature(BODY.replace(b":", b": "), sign(BODY, SECRET), SECRET)


def test_signature_wrong_secret() -> None:
    assert not verify_signature(BODY, sign(BODY, "other-secret"), SECRET)


def test_signature_missing_header() -> None:
    assert not verify_signature(BODY, None, SECRET)


def test_signature_wrong_prefix() -> None:
    header = sign(BODY, SECRET).replace("sha256=", "sha1=")
    assert not verify_signature(BODY, header, SECRET)
