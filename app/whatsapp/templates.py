"""Approved message templates: catalog, validation, and rendering (CLAUDE.md §4).

Templates are the only messages WhatsApp allows outside the 24-hour
customer-service window, so every feature that writes first uses one:
reminders, lead follow-ups, staff alerts, conversation re-open, and booking
updates. Their definitions live in ``templates/whatsapp/*.json`` (the same
payload that was submitted to Meta on day 1), one file per template holding
the Arabic and the English version.

This module:
    * loads those files into a ``TemplateCatalog``;
    * checks at startup that every ``TEMPLATE_*`` name in config exists in both
      languages with the parameter count the code sends (a mismatch would make
      Meta reject every send with error 132000), so a typo stops the app instead
      of silently dropping reminders;
    * renders the body text, so the inbox thread shows what the customer got.

Sending is ``WhatsAppClient.send_template``. Nothing here calls Meta.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "whatsapp"
LANGUAGES = ("ar", "en")
_PARAM = re.compile(r"\{\{(\d+)\}\}")


class TemplateError(ValueError):
    """A template file is malformed, or config names a template that isn't defined."""


@dataclass(frozen=True, slots=True)
class TemplateDef:
    """One language version of one template."""

    name: str
    language: str
    category: str
    body: str
    param_count: int
    quick_replies: tuple[str, ...]  # button titles, in order

    def render(self, params: list[str]) -> str:
        """The body with ``{{n}}`` replaced, as the customer sees it."""
        return _PARAM.sub(lambda m: params[int(m.group(1)) - 1], self.body)


# Parameters each config key's template must take, in the order the code sends
# them (see templates/whatsapp/README.md). Quick-reply counts likewise.
EXPECTED: dict[str, tuple[int, int]] = {
    "template_reminder": (3, 2),  # business, day, time; Confirm / Reschedule
    "template_booking_update": (3, 0),  # business, day, time
    "template_staff_alert": (2, 0),  # customer first name, reason
    "template_reopen": (2, 0),  # customer first name, business
    "template_followup": (3, 0),  # customer first name, service, business
}


def _parse_entry(entry: dict[str, Any], source: Path) -> TemplateDef:
    try:
        name, language, category = entry["name"], entry["language"], entry["category"]
        components = entry["components"]
    except (KeyError, TypeError) as exc:
        raise TemplateError(f"{source}: entry missing {exc}") from exc
    body = next((c.get("text", "") for c in components if c.get("type") == "BODY"), "")
    if not body:
        raise TemplateError(f"{source}: {name}/{language} has no BODY text")
    numbers = sorted({int(n) for n in _PARAM.findall(body)})
    if numbers != list(range(1, len(numbers) + 1)):
        raise TemplateError(f"{source}: {name}/{language} parameters must be {{{{1}}}}..{{{{n}}}}")
    buttons: list[dict[str, Any]] = next(
        (c.get("buttons", []) for c in components if c.get("type") == "BUTTONS"), []
    )
    quick = tuple(b.get("text", "") for b in buttons if b.get("type") == "QUICK_REPLY")
    return TemplateDef(name, language, category, body, len(numbers), quick)


class TemplateCatalog:
    """Every template definition, looked up by (name, language)."""

    def __init__(self, defs: list[TemplateDef]) -> None:
        self._defs = {(d.name, d.language): d for d in defs}

    @classmethod
    def load(cls, directory: Path = TEMPLATES_DIR) -> TemplateCatalog:
        """Read every ``*.json`` in ``directory``.

        Raises:
            TemplateError: The directory is missing, or a file isn't valid.
        """
        if not directory.is_dir():
            raise TemplateError(f"template directory not found: {directory}")
        defs: list[TemplateDef] = []
        for path in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TemplateError(f"{path}: invalid JSON: {exc}") from exc
            entries = raw if isinstance(raw, list) else [raw]
            defs.extend(_parse_entry(e, path) for e in entries)
        return cls(defs)

    def get(self, name: str, language: str) -> TemplateDef:
        """The definition for ``name`` in ``language``.

        Raises:
            TemplateError: Not defined.
        """
        try:
            return self._defs[(name, language)]
        except KeyError as exc:
            raise TemplateError(f"template {name!r} has no {language!r} version") from exc

    def validate(self, settings: Settings) -> None:
        """Check every ``TEMPLATE_*`` config name against the definitions (startup, §8).

        Raises:
            TemplateError: A name is missing in a language, or takes a different
                number of parameters or quick replies than the code sends.
        """
        for key, (params, quick) in EXPECTED.items():
            name = str(getattr(settings, key))
            for language in LANGUAGES:
                d = self.get(name, language)
                if d.param_count != params or len(d.quick_replies) != quick:
                    raise TemplateError(
                        f"{key.upper()}={name!r} ({language}) takes {d.param_count} parameters "
                        f"and {len(d.quick_replies)} quick replies; the code sends {params} "
                        f"and {quick}. Update templates/whatsapp or the sender."
                    )

    def render(self, name: str, language: str, params: list[str]) -> str:
        """The body text of ``name`` in ``language`` with ``params`` filled in."""
        return self.get(name, language).render(params)


def load_catalog(settings: Settings, directory: Path = TEMPLATES_DIR) -> TemplateCatalog:
    """Load and validate the catalog in one step. Used by every process at startup.

    Raises:
        TemplateError: See ``TemplateCatalog.load`` and ``TemplateCatalog.validate``.
    """
    catalog = TemplateCatalog.load(directory)
    catalog.validate(settings)
    return catalog
