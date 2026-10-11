"""Load and validate the knowledge base (CLAUDE.md §5).

Three files, all filled in from the client's order:

* ``business.yaml``: business info, opening hours, services (id, names in
  Arabic and English, duration, price), staff, and booking rules.
* ``faqs.yaml``: Q&As, each in both languages. No count limit.
* ``style_ar.md``: the client-approved Arabic dialect and tone examples.

All are validated at startup. A bad file stops the app instead of producing
wrong answers or wrong bookings: missing fields, unknown staff ids, malformed
hours, or template ``{{PLACEHOLDERS}}`` that were never filled in.

Size: below ~15k tokens the whole knowledge base goes into the cached system
prompt (§5). Above that, a warning is logged: switch on retrieval then.

The rendered text goes into the system prompt, so it must be byte-identical
between requests (prompt caching). Nothing here depends on the clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.logging import get_logger

_PLACEHOLDER = "{{"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_RANGE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
# Above this the prompt gets expensive and the model starts missing facts (§5).
PROMPT_TOKEN_BUDGET = 15_000

log = get_logger(__name__)


class KnowledgeError(ValueError):
    """A knowledge file is missing, malformed, or still has template placeholders."""


@dataclass(frozen=True, slots=True)
class OpenRange:
    """One opening period in local time, e.g. 09:00-13:00."""

    start: time
    end: time


def parse_hours(value: str) -> list[OpenRange]:
    """Parse ``"09:00-13:00, 16:00-22:00"`` or ``"closed"`` into ranges.

    Raises:
        ValueError: The text isn't a list of ``HH:MM-HH:MM`` ranges, a range
            ends before it starts, or ranges overlap.
    """
    text = value.strip().lower()
    if text == "closed":
        return []
    ranges: list[OpenRange] = []
    for part in text.split(","):
        m = _RANGE.match(part.strip())
        if not m:
            raise ValueError(f"hours must look like 09:00-21:00 or 'closed', got {value!r}")
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if h2 == 24 and m2 == 0:  # "…-24:00" means until midnight
            h2, m2 = 23, 59
        start, end = time(h1, m1), time(h2, m2)
        if end <= start:
            raise ValueError(
                f"range {part.strip()!r} ends before it starts (overnight not supported)"
            )
        if ranges and start < ranges[-1].end:
            raise ValueError(f"ranges in {value!r} overlap or are out of order")
        ranges.append(OpenRange(start, end))
    return ranges


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class Business(_Strict):
    name_ar: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    description_ar: str = ""
    description_en: str = ""
    location: str = Field(min_length=1)
    maps_link: str = ""
    # Every weekday, in local time (BUSINESS_TIMEZONE). Also the bookable hours.
    hours: dict[str, str]
    hours_note: str = ""

    @field_validator("hours")
    @classmethod
    def _all_days_valid(cls, hours: dict[str, str]) -> dict[str, str]:
        hours = {k.strip().lower(): v for k, v in hours.items()}
        missing = [d for d in WEEKDAYS if d not in hours]
        unknown = [d for d in hours if d not in WEEKDAYS]
        if missing or unknown:
            raise ValueError(
                f"hours needs exactly the 7 weekdays; missing {missing}, unknown {unknown}"
            )
        for value in hours.values():
            parse_hours(value)
        return hours

    def open_ranges(self, weekday: int) -> list[OpenRange]:
        """Opening ranges for ``date.weekday()`` (0 = Monday)."""
        return parse_hours(self.hours[WEEKDAYS[weekday]])


class BookingRules(_Strict):
    buffer_minutes: int = Field(default=0, ge=0, le=240)  # gap after each appointment
    min_lead_time_hours: float = Field(default=0, ge=0)  # earliest bookable time from now
    booking_horizon_days: int = Field(default=30, ge=1, le=365)  # how far ahead
    slot_interval_minutes: int = Field(default=30, ge=5, le=240)  # start times every N minutes
    # Cancelling or moving a booking closer than this to its start is refused (0 = any time).
    min_cancel_notice_hours: float = Field(default=0, ge=0)
    cancellation_policy_ar: str = Field(min_length=1)
    cancellation_policy_en: str = Field(min_length=1)


class Service(_Strict):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name_ar: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    duration_minutes: int = Field(ge=5, le=600)
    price_sar: float | None = Field(default=None, ge=0)  # None = "price on request"
    price_note_ar: str = ""  # e.g. "يبدأ من"
    price_note_en: str = ""  # e.g. "from"
    staff_ids: list[str] = []  # empty = any staff member (or the business itself if no staff)


class Staff(_Strict):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name_ar: str = Field(min_length=1)
    name_en: str = Field(min_length=1)


class FAQ(_Strict):
    q_ar: str = Field(min_length=1)
    a_ar: str = Field(min_length=1)
    q_en: str = Field(min_length=1)
    a_en: str = Field(min_length=1)


class BusinessFile(_Strict):
    business: Business
    booking_rules: BookingRules
    services: list[Service] = Field(min_length=1)
    staff: list[Staff] = []

    @model_validator(mode="after")
    def _references(self) -> BusinessFile:
        service_ids = [s.id for s in self.services]
        staff_ids = [s.id for s in self.staff]
        for label, ids in (("service", service_ids), ("staff", staff_ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} ids: {dupes}")
        for svc in self.services:
            unknown = [i for i in svc.staff_ids if i not in staff_ids]
            if unknown:
                raise ValueError(f"service {svc.id!r} lists unknown staff ids {unknown}")
        return self


class FaqFile(_Strict):
    faqs: list[FAQ] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    """Everything the assistant may say or book, validated."""

    business: Business
    rules: BookingRules
    services: tuple[Service, ...]
    staff: tuple[Staff, ...]
    faqs: tuple[FAQ, ...]
    style_guide: str = ""  # knowledge/style_ar.md, verbatim

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def staff_member(self, staff_id: str) -> Staff | None:
        return next((s for s in self.staff if s.id == staff_id), None)

    def staff_for(self, service: Service) -> list[str | None]:
        """Staff ids who can deliver ``service``. ``[None]`` when the business has no staff list."""
        if service.staff_ids:
            return list(service.staff_ids)
        if self.staff:
            return [s.id for s in self.staff]
        return [None]


def _strings(value: Any) -> list[str]:
    """Every string inside a nested dict/list structure."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _read(path: str | Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KnowledgeError(f"knowledge file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise KnowledgeError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise KnowledgeError(f"{path}: expected a YAML mapping at the top level")
    unfilled = [s for s in _strings(raw) if _PLACEHOLDER in s]
    if unfilled:
        raise KnowledgeError(f"{path}: unfilled template placeholders: {unfilled[:3]}")
    return raw


def _read_style_guide(path: str | Path | None) -> str:
    if path is None:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise KnowledgeError(f"style guide not found: {path}") from exc
    if _PLACEHOLDER in text:
        raise KnowledgeError(f"{path}: unfilled template placeholders (look for '{{{{')")
    return text


def estimate_tokens(text: str) -> int:
    """Rough token count: ~3.5 characters per token across Arabic and English.

    Good enough to decide when to switch on retrieval; not for billing.
    """
    return round(len(text) / 3.5)


def load_knowledge(
    business_path: str | Path,
    faq_path: str | Path,
    style_guide_path: str | Path | None = None,
) -> KnowledgeBase:
    """Read, parse, and validate the knowledge files.

    Args:
        business_path: ``BUSINESS_CONFIG_PATH``.
        faq_path: ``FAQ_PATH``.
        style_guide_path: ``STYLE_GUIDE_PATH``. ``None`` skips it (some tests).

    Returns:
        The validated knowledge base. Logs a warning when the rendered prompt
        text exceeds ``PROMPT_TOKEN_BUDGET`` (time to switch on retrieval, §5).

    Raises:
        KnowledgeError: With a message saying exactly what to fix.
    """
    try:
        biz = BusinessFile.model_validate(_read(business_path))
    except ValidationError as exc:
        raise KnowledgeError(f"{business_path}: {exc}") from exc
    try:
        faq = FaqFile.model_validate(_read(faq_path))
    except ValidationError as exc:
        raise KnowledgeError(f"{faq_path}: {exc}") from exc
    kb = KnowledgeBase(
        business=biz.business,
        rules=biz.booking_rules,
        services=tuple(biz.services),
        staff=tuple(biz.staff),
        faqs=tuple(faq.faqs),
        style_guide=_read_style_guide(style_guide_path),
    )
    tokens = estimate_tokens(render_knowledge(kb) + kb.style_guide)
    if tokens > PROMPT_TOKEN_BUDGET:
        log.warning(
            "knowledge base is large; consider retrieval (CLAUDE.md §5)",
            extra={"event": "kb_large", "count": tokens},
        )
    return kb


def _price(svc: Service) -> str:
    if svc.price_sar is None:
        return "price on request"
    amount = f"{svc.price_sar:g} SAR"
    note = " / ".join(n for n in (svc.price_note_en, svc.price_note_ar) if n)
    return f"{amount} ({note})" if note else amount


def render_knowledge(kb: KnowledgeBase) -> str:
    """Render the knowledge base as plain text for the system prompt.

    Depends only on the files, so it is byte-identical between requests.
    """
    b, r = kb.business, kb.rules
    lines = [
        f"Business name: {b.name_en} / {b.name_ar}",
    ]
    if b.description_en or b.description_ar:
        lines.append(f"About: {b.description_en} / {b.description_ar}".strip(" /"))
    lines.append(f"Location: {b.location}")
    if b.maps_link:
        lines.append(f"Google Maps: {b.maps_link}")
    lines.append("Opening hours (local time; also the bookable hours):")
    lines.extend(f"- {day.capitalize()}: {b.hours[day]}" for day in WEEKDAYS)
    if b.hours_note:
        lines.append(f"Hours note: {b.hours_note}")

    lines += ["", "Services (use the id with the booking tools):"]
    for svc in kb.services:
        who = ", ".join(
            f"{st.name_en} / {st.name_ar} (staff_id {st.id})"
            for sid in svc.staff_ids
            if (st := kb.staff_member(sid))
        )
        lines.append(
            f"- id {svc.id}: {svc.name_en} / {svc.name_ar}; {svc.duration_minutes} min; "
            f"{_price(svc)}" + (f"; with {who}" if who else "")
        )
    if kb.staff:
        lines += ["", "Staff:"]
        lines.extend(f"- staff_id {s.id}: {s.name_en} / {s.name_ar}" for s in kb.staff)

    lines += [
        "",
        "Booking rules:",
        f"- Book at least {r.min_lead_time_hours:g} hours ahead and at most "
        f"{r.booking_horizon_days} days ahead.",
        f"- Cancellation policy (English): {r.cancellation_policy_en}",
        f"- Cancellation policy (Arabic): {r.cancellation_policy_ar}",
    ]
    if r.min_cancel_notice_hours:
        lines.append(
            f"- Bookings can't be cancelled or moved less than {r.min_cancel_notice_hours:g} "
            "hours before they start."
        )

    lines += ["", "FAQ (each in English and Arabic):"]
    for i, qa in enumerate(kb.faqs, 1):
        lines += [
            f"{i}. Q (en): {qa.q_en}",
            f"   A (en): {qa.a_en}",
            f"   Q (ar): {qa.q_ar}",
            f"   A (ar): {qa.a_ar}",
        ]
    return "\n".join(lines)
