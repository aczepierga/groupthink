"""Fine-print facts. Tri-state: None = unknown (no fine print), False = fine print read, no match.

Patterns are ported from runs/docs/fineprint_patterns.json (185 offers, 570 lines) and
tightened where the raw category over-matched (e.g. "group_size" fired on 153 of 185 offers).
"""
import re
from dataclasses import dataclass

_I = re.IGNORECASE

LIMIT_RE = re.compile(r"\blimit\s+(\d+|one)\s+(?:(?:redemption|voucher|purchase)s?\s+)?per\s+person", _I)
GIFT_RE = re.compile(r"\bmay\s+buy\s+\d+\s+additional\s+as\s+gifts?\b", _I)
# "Appointment required", "Appointment/Reservation required", "Reservations required",
# "Reservations are strictly mandatory". A "not" between the noun and "required" blocks it.
APPOINTMENT_RE = re.compile(
    r"\b(?:appointments?|reservations?)\b(?:(?!\bnot\b|n't|\.)[^.]){0,40}?\b(?:required|mandatory)\b"
    r"|\bmust\s+(?:make|book|reserve|schedule)\s+(?:a\s+|an\s+)?(?:reservation|appointment)\b"
    r"|\breservations?\s+must\s+be\s+made\b|\bno\s+reservation\s*=\s*no\s+entry\b", _I)
AGE_21_RE = re.compile(
    r"\b21\s*\+|\b21\s+(?:or\s+(?:older|over)|and\s+(?:older|over)|years)|\bage[sd]?\s*21\b|\bunder\s+(?:the\s+age\s+of\s+)?21\b", _I)
NEW_CUSTOMERS_RE = re.compile(
    r"\bnew\s+(?:customers?|clients?)\s+only\b|\bfirst[- ]time\s+(?:customers?|clients?|guests?)\b", _I)
_DAYS_TO = r"(?:monday|mon)\s*(?:-|to|through|thru)\s*(?:thursday|thu|friday|fri)"
WEEKDAY_RE = re.compile(
    r"\b(?:valid|available|redeemable)\s+(?:only\s+)?(?:on\s+)?(?:weekdays?\b|%s)|\bweekdays?\s+only\b" % _DAYS_TO, _I)
COMBINE_RE = re.compile(
    r"\b(?:cannot|can\s*not|can't|may\s+not|not)\s+be\s+(?:combined|used\s+in\s+conjunction)\b"
    r"|\bnot\s+valid\s+(?:with|in\s+conjunction\s+with)\s+(?:any\s+)?other\s+(?:offers?|promotions?|discounts?|specials?|vouchers?)\b", _I)
TAX_TIP_RE = re.compile(
    r"\b(?:tax|gratuity|tip|service\s+charge)\b[^.]{0,40}\b(?:not\s+included|extra|additional|will\s+be\s+(?:collected|due)|due|charged)\b"
    r"|\b(?:tax|gratuity|tip)(?:\s+and\s+(?:tax|gratuity|tip))?\s+(?:is|are)?\s*not\s+included\b"
    r"|\b\d+%\s+(?:gratuity|service\s+charge|tip)\b|\bmandatory\s+(?:\d+%\s+)?(?:gratuity|service\s+charge|tip)\b", _I)


@dataclass
class Facts:
    limit_per_person: int | None = None
    appointment: bool | None = None
    age_21: bool | None = None
    gift_ok: bool | None = None
    new_customers_only: bool | None = None
    weekday_only: bool | None = None
    cannot_combine: bool | None = None
    tax_tip_extra: bool | None = None


def extract_facts(fine_print: list[str] | None) -> Facts:
    """None -> every fact None (unknown). A list (even empty) -> bools, False when nothing matched.

    limit_per_person is an int; with no "Limit N per person" line it stays None (no stated limit),
    since False would read as 0.
    """
    if fine_print is None:
        return Facts()
    lines = [s for s in fine_print if isinstance(s, str)]

    def hit(rx):
        return any(rx.search(s) for s in lines)

    limits = [1 if m.group(1).lower() == "one" else int(m.group(1)) for s in lines for m in LIMIT_RE.finditer(s)]
    return Facts(
        limit_per_person=min(limits) if limits else None,
        appointment=hit(APPOINTMENT_RE),
        age_21=hit(AGE_21_RE),
        gift_ok=hit(GIFT_RE),
        new_customers_only=hit(NEW_CUSTOMERS_RE),
        weekday_only=hit(WEEKDAY_RE),
        cannot_combine=hit(COMBINE_RE),
        tax_tip_extra=hit(TAX_TIP_RE),
    )
