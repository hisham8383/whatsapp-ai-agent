"""Knowledge validation (CLAUDE.md §5): refuse to start on anything wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.kb.loader import KnowledgeError, load_knowledge, parse_hours, render_knowledge
from tests.conftest import ROOT

DEMO = ROOT / "examples" / "demo-clinic"
BUSINESS = (DEMO / "business.yaml").read_text(encoding="utf-8")
FAQS = (DEMO / "faqs.yaml").read_text(encoding="utf-8")


def _write(tmp_path: Path, business: str = BUSINESS, faqs: str = FAQS) -> tuple[Path, Path]:
    b, f = tmp_path / "business.yaml", tmp_path / "faqs.yaml"
    b.write_text(business, encoding="utf-8")
    f.write_text(faqs, encoding="utf-8")
    return b, f


def test_demo_loads() -> None:
    kb = load_knowledge(DEMO / "business.yaml", DEMO / "faqs.yaml")
    assert [s.id for s in kb.services] == ["checkup", "cleaning", "whitening", "kids-checkup"]
    assert len(kb.faqs) == 10
    cleaning = kb.service("cleaning")
    assert cleaning is not None
    assert kb.staff_for(cleaning) == ["dr-sara"]


def test_template_placeholders_refuse_to_start() -> None:
    with pytest.raises(KnowledgeError, match="unfilled template placeholders"):
        load_knowledge(ROOT / "knowledge" / "business.yaml", DEMO / "faqs.yaml")
    with pytest.raises(KnowledgeError, match="unfilled template placeholders"):
        load_knowledge(DEMO / "business.yaml", ROOT / "knowledge" / "faqs.yaml")


def _faqs(count: int, answer_ar: str = "ج") -> str:
    rows = [
        f'  - {{q_ar: "س{i}", a_ar: "{answer_ar}{i}", q_en: "Q{i}", a_en: "A{i}"}}'
        for i in range(count)
    ]
    return "\n".join(["faqs:", *rows]) + "\n"


def test_no_qa_limit(tmp_path: Path) -> None:
    b, f = _write(tmp_path, faqs=_faqs(150))
    assert len(load_knowledge(b, f).faqs) == 150


def test_style_guide_loaded_and_placeholders_refused(tmp_path: Path) -> None:
    kb = load_knowledge(DEMO / "business.yaml", DEMO / "faqs.yaml", DEMO / "style_ar.md")
    assert "حياك" in kb.style_guide
    with pytest.raises(KnowledgeError, match="unfilled template placeholders"):
        load_knowledge(
            DEMO / "business.yaml", DEMO / "faqs.yaml", ROOT / "knowledge" / "style_ar.md"
        )
    with pytest.raises(KnowledgeError, match="style guide not found"):
        load_knowledge(DEMO / "business.yaml", DEMO / "faqs.yaml", tmp_path / "missing.md")


def test_large_knowledge_base_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    b, f = _write(tmp_path, faqs=_faqs(40, answer_ar="x" * 2000))
    with caplog.at_level("WARNING"):
        load_knowledge(b, f)
    assert "consider retrieval" in caplog.text


def test_unknown_staff_id_rejected(tmp_path: Path) -> None:
    b, f = _write(tmp_path, BUSINESS.replace('staff_ids: ["dr-sara"]', 'staff_ids: ["dr-x"]', 1))
    with pytest.raises(KnowledgeError, match="unknown staff ids"):
        load_knowledge(b, f)


def test_missing_weekday_rejected(tmp_path: Path) -> None:
    b, f = _write(tmp_path, BUSINESS.replace('    friday: "closed"\n', ""))
    with pytest.raises(KnowledgeError, match="7 weekdays"):
        load_knowledge(b, f)


def test_missing_translation_rejected(tmp_path: Path) -> None:
    b, f = _write(tmp_path, faqs=FAQS.replace('    q_en: "Is there parking?"\n', ""))
    with pytest.raises(KnowledgeError, match="q_en"):
        load_knowledge(b, f)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("09:00-21:00", True),
        ("09:00-13:00, 16:00-22:00", True),
        ("closed", True),
        ("16:00-24:00", True),
        ("21:00-09:00", False),
        ("9am-5pm", False),
        ("09:00-13:00, 12:00-15:00", False),
    ],
)
def test_parse_hours(text: str, ok: bool) -> None:
    if ok:
        parse_hours(text)
    else:
        with pytest.raises(ValueError):
            parse_hours(text)


def test_render_is_stable_and_complete() -> None:
    kb = load_knowledge(DEMO / "business.yaml", DEMO / "faqs.yaml")
    text = render_knowledge(kb)
    assert text == render_knowledge(kb)  # byte-stable for prompt caching
    assert "id cleaning: Teeth cleaning / تنظيف الأسنان; 45 min; 250 SAR" in text
    assert "Friday: closed" in text
    assert "Q (ar): هل فيه مواقف سيارات؟" in text
