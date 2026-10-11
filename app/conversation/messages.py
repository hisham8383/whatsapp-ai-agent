"""Customer messages about booking changes, written by code, never by the model (CLAUDE.md §5).

Two moments in a booking are too important to leave to the model's wording:

* **Asking to confirm.** The message lists exactly what will be booked
  (service, staff, day, date, time, duration, price) and offers
  "Confirm / Change" reply buttons. It can never read as if the booking were
  already made.
* **Saying it's done.** Sent only after the booking system accepted the change.

Both are built from the knowledge base and the tool's own data, in the
customer's language.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.conversation.replier import Reply
from app.conversation.timefmt import long_label
from app.conversation.tools import ChangeInfo
from app.kb.loader import KnowledgeBase
from app.whatsapp.client import ListRow

CONFIRM_ID = "confirm"
CHANGE_ID = "change"

_BUTTONS = {
    ("create_booking", "ar"): ("نعم، أكّد الحجز", "تعديل"),
    ("create_booking", "en"): ("Yes, book it", "Change"),
    ("reschedule_booking", "ar"): ("نعم، غيّر الموعد", "تعديل"),
    ("reschedule_booking", "en"): ("Yes, move it", "Change"),
    ("cancel_booking", "ar"): ("نعم، ألغِ الموعد", "لا، أبقِه"),
    ("cancel_booking", "en"): ("Yes, cancel it", "No, keep it"),
}


def _what(info: ChangeInfo, kb: KnowledgeBase, language: str) -> str:
    """``Teeth cleaning with Dr. Sara`` / ``تنظيف الأسنان مع د. سارة``."""
    svc = info.service.name_ar if language == "ar" else info.service.name_en
    staff = kb.staff_member(info.staff_id) if info.staff_id else None
    if staff is None:
        return svc
    name = staff.name_ar if language == "ar" else staff.name_en
    return f"{svc} {'مع' if language == 'ar' else 'with'} {name}"


def _terms(info: ChangeInfo, language: str) -> str:
    """Duration and price line, e.g. ``45 min · 250 SAR``."""
    svc = info.service
    if language == "ar":
        parts = [f"المدة {svc.duration_minutes} دقيقة"]
        if svc.price_sar is not None:
            parts.append(f"السعر {svc.price_sar:g} ريال")
    else:
        parts = [f"{svc.duration_minutes} min"]
        if svc.price_sar is not None:
            parts.append(f"{svc.price_sar:g} SAR")
    return " · ".join(parts)


def confirmation(info: ChangeInfo, kb: KnowledgeBase, tz: ZoneInfo, language: str) -> Reply:
    """The "please confirm" message with Confirm / Change buttons."""
    ar = language == "ar"
    what, when = _what(info, kb, language), long_label(info.start, tz, language)
    if info.action == "create_booking":
        text = (
            f"فضلًا أكّد الحجز:\n{what}\n{when}\n{_terms(info, language)}"
            if ar
            else f"Please confirm your booking:\n{what}\n{when}\n{_terms(info, language)}"
        )
    elif info.action == "reschedule_booking":
        old = long_label(info.old_start or info.start, tz, language)
        text = (
            f"فضلًا أكّد تغيير الموعد:\n{what}\nمن: {old}\nإلى: {when}"
            if ar
            else f"Please confirm the change:\n{what}\nFrom: {old}\nTo: {when}"
        )
    else:
        policy = kb.rules.cancellation_policy_ar if ar else kb.rules.cancellation_policy_en
        text = (
            f"فضلًا أكّد إلغاء الموعد:\n{what}\n{when}\n\n{policy}"
            if ar
            else f"Please confirm the cancellation:\n{what}\n{when}\n\n{policy}"
        )
    yes, no = _BUTTONS[(info.action, "ar" if ar else "en")]
    return Reply(text, buttons=(ListRow(CONFIRM_ID, yes), ListRow(CHANGE_ID, no)))


def done(info: ChangeInfo, kb: KnowledgeBase, tz: ZoneInfo, language: str) -> Reply:
    """The message sent after the booking system accepted the change."""
    ar = language == "ar"
    what, when = _what(info, kb, language), long_label(info.start, tz, language)
    if info.action == "create_booking":
        return Reply(
            f"تم الحجز ✅\n{what}\n{when}\nبانتظارك!"
            if ar
            else f"You're booked ✅\n{what}\n{when}\nSee you then!"
        )
    if info.action == "reschedule_booking":
        return Reply(
            f"تم تغيير الموعد ✅\n{what}\n{when}"
            if ar
            else f"Your appointment is moved ✅\n{what}\n{when}"
        )
    return Reply(
        f"تم إلغاء الموعد ✅\n{what}\n{when}"
        if ar
        else f"Your appointment is cancelled ✅\n{what}\n{when}"
    )
