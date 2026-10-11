"""Opt-out detection (CLAUDE.md §4).

A customer who opts out must never receive another ``lead_followup``.
Reminders for existing bookings still go out unless the customer opts out of
reminders specifically ("stop reminders", "ايقاف التذكير").

We match the whole message (after normalization), not a substring, so
"don't stop the booking" is not treated as an opt-out.

"الغاء" / "cancel" on its own is ambiguous: it usually means "cancel my
booking". It counts as an opt-out only as the reply to a follow-up; the
caller decides that (``Classification.CANCEL_WORD``), everything else goes to
the assistant.

Arabic normalization folds the spelling variants people actually type:
alef forms (أ إ آ → ا), taa marbuta (ة → ه), alef maqsura (ى → ي),
and diacritics/tatweel are removed. So "إلغاء الاشتراك", "الغاء الاشتراك" and
"الغاء الأشتراك" all match.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

_DIACRITICS = re.compile(r"[ً-ْٰـ]")  # harakat, dagger alef, tatweel
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

# All phrase sets are stored in normalized form, so they must stay in sync with normalize().
OPT_OUT_PHRASES: frozenset[str] = frozenset(
    {
        # English
        "stop",
        "unsubscribe",
        "stop messages",
        "stop all",
        "opt out",
        "optout",
        "dont message me",
        "don t message me",  # "don't" after punctuation is stripped
        "do not message me",
        # Arabic
        "ايقاف",
        "وقف",
        "توقف",
        "الغاء الاشتراك",
        "الغاء اشتراك",
        "ايقاف الرسائل",
        "لا ترسل",
        "لا ترسلون",
        "لا ترسلوا",
        "لا ترسل لي",
        "لا ترسلون لي",
        "لا تراسلوني",
        "لا تراسلني",
    }
)

REMINDER_OPT_OUT_PHRASES: frozenset[str] = frozenset(
    {
        "stop reminders",
        "no reminders",
        "no more reminders",
        "ايقاف التذكير",
        "ايقاف التذكيرات",
        "وقف التذكير",
        "لا ترسل تذكير",
        "لا ترسلون تذكير",
        "لا ترسل تذكيرات",
        "بدون تذكير",
        "ما ابي تذكير",
    }
)

# Opt-out only as the reply to a lead follow-up; otherwise a booking request.
CANCEL_WORDS: frozenset[str] = frozenset({"الغاء", "cancel"})


class Classification(StrEnum):
    """What an inbound message means for opt-outs."""

    NONE = "none"
    FOLLOWUPS = "followups"  # stop lead follow-ups (and any other marketing)
    REMINDERS = "reminders"  # stop appointment reminders
    CANCEL_WORD = "cancel_word"  # bare "cancel": opt-out only right after a follow-up


def normalize(text: str) -> str:
    """Lower-case, fold Arabic variants, and strip diacritics, punctuation, and extra spaces."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _DIACRITICS.sub("", text).translate(_ARABIC_FOLD)
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def classify(text: str | None) -> Classification:
    """Classify a whole inbound message.

    Args:
        text: The customer's message, or ``None`` for non-text messages.

    Returns:
        ``NONE`` for anything that isn't an exact opt-out phrase.
    """
    if not text:
        return Classification.NONE
    norm = normalize(text)
    if norm in REMINDER_OPT_OUT_PHRASES:
        return Classification.REMINDERS
    if norm in OPT_OUT_PHRASES:
        return Classification.FOLLOWUPS
    if norm in CANCEL_WORDS:
        return Classification.CANCEL_WORD
    return Classification.NONE


def is_opt_out(text: str | None) -> bool:
    """True if the whole message is an unambiguous follow-up opt-out request."""
    return classify(text) is Classification.FOLLOWUPS
