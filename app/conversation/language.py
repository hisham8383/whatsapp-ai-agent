"""Reply-language detection (CLAUDE.md §5: reply in the customer's language)."""

from __future__ import annotations

import re

_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")


def detect_language(text: str | None, fallback: str = "ar") -> str:
    """Return ``"ar"`` if the text contains Arabic script, ``"en"`` if it has Latin letters.

    Args:
        text: Customer message. ``None`` or text without letters falls back.
        fallback: Language to use when nothing can be detected, usually the
            customer's stored language or ``DEFAULT_LANGUAGE``.
    """
    if not text:
        return fallback
    if _ARABIC.search(text):
        return "ar"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return fallback
