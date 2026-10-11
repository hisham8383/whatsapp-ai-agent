"""Template catalog: the day-1 definitions match what the code sends (CLAUDE.md §4, §8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.whatsapp.templates import TEMPLATES_DIR, TemplateCatalog, TemplateError, load_catalog


def test_repo_templates_match_config(settings: Settings) -> None:
    catalog = load_catalog(settings)
    reminder = catalog.get("appointment_reminder", "en")
    assert reminder.category == "UTILITY"
    assert reminder.quick_replies == ("Confirm", "Reschedule")
    assert catalog.get("lead_followup", "ar").category == "MARKETING"
    # Every template exists in both languages (§4: Arabic + English each).
    names = {json.loads(p.read_text("utf-8"))[0]["name"] for p in TEMPLATES_DIR.glob("*.json")}
    assert names == {
        "appointment_reminder",
        "booking_confirmation_update",
        "staff_handoff_alert",
        "conversation_reopen",
        "lead_followup",
    }


def test_render(settings: Settings) -> None:
    text = load_catalog(settings).render(
        "appointment_reminder", "en", ["Demo Clinic", "Monday 5 October", "10:00"]
    )
    assert text.startswith("Reminder: your appointment at Demo Clinic is on Monday 5 October")


def test_unknown_template_name_refuses_to_start(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "template_followup", "lead_follow_up_v2")
    with pytest.raises(TemplateError, match="lead_follow_up_v2"):
        load_catalog(settings)


def test_parameter_mismatch_refuses_to_start(settings: Settings, tmp_path: Path) -> None:
    for path in TEMPLATES_DIR.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text("utf-8"), "utf-8")
    alert = tmp_path / "staff_handoff_alert.json"
    alert.write_text(alert.read_text("utf-8").replace("{{2}}", "x"), "utf-8")
    with pytest.raises(TemplateError, match="takes 1 parameters"):
        load_catalog(settings, tmp_path)


def test_gapped_parameters_rejected(tmp_path: Path) -> None:
    (tmp_path / "t.json").write_text(
        json.dumps(
            [
                {
                    "name": "t",
                    "language": "en",
                    "category": "UTILITY",
                    "components": [{"type": "BODY", "text": "{{1}} and {{3}}"}],
                }
            ]
        ),
        "utf-8",
    )
    with pytest.raises(TemplateError, match="parameters"):
        TemplateCatalog.load(tmp_path)
