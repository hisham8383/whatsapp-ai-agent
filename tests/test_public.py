"""Public pages: privacy policy (required by Meta to publish the app) and the health alias."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_privacy_page_is_public_and_bilingual(client: TestClient) -> None:
    r = client.get("/privacy")
    assert r.status_code == 200
    page = r.text
    assert "Ibtisama Demo Dental Clinic" in page and "عيادة ابتسامة التجريبية" in page
    assert "deleted automatically after 90 days" in page  # LOG_RETENTION_DAYS
    assert 'id="deletion"' in page  # Meta's data-deletion instructions
    assert "Anthropic" in page and "Google Cloud" in page


def test_health_alias(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
