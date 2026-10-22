"""Fit engine: deterministic ranking of candidates against a group. The debate only narrates this.

Hard filters (hard-no word, age 21 with kids, quantity over max_units) exclude. Budget is a score
term, not a filter: over-budget options stay in the shortlist as debate material but never in `top`.
Member text is untrusted: truncated, keyword-matched with escaped word-boundary patterns only.
"""
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date

from .candidates import Candidate, title_party  # noqa: F401  (title_party is part of this module's interface)

NAME_MAX = 40
TEXT_MAX = 280
LIST_MAX = 20
SHORTLIST_N = 5
TOP_N = 3


def _text(v, limit: int) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def _text_list(v, limit: int = TEXT_MAX) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    out = [_text(s, limit) for s in v]
    return [s for s in out if s][:LIST_MAX]


@dataclass
class Member:
    id: str
    name: str
    max_budget_minor: int
    likes: list[str]
    hard_nos: list[str]
    kids: bool
    constraints: str
    share_numbers: bool
    sample: bool = False   # a labelled demo person: argues, but is not a voter

    @classmethod
    def clean(cls, id, name, max_budget_minor, likes, hard_nos, kids, constraints,
              share_numbers, sample=False) -> "Member":
        """Build a Member from untrusted input. Truncates, drops empties, never raises."""
        try:
            budget = max(0, int(max_budget_minor))
        except (TypeError, ValueError, OverflowError):
            budget = 0
        return cls(id=_text(str(id) if id is not None else "", 64), name=_text(name, NAME_MAX),
                   max_budget_minor=budget, likes=_text_list(likes), hard_nos=_text_list(hard_nos),
                   kids=bool(kids), constraints=_text(constraints, TEXT_MAX),
                   share_numbers=bool(share_numbers), sample=bool(sample))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Member":
        return cls(**d)


@dataclass
class Verdict:
    member_id: str
    fits_budget: bool
    over_by_minor: int
    likes_matched: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Verdict":
        return cls(**d)


@dataclass
class Ranked:
    candidate: Candidate
    quantity: int
    verdicts: list[Verdict]
    score: float
    discount_pct: int | None
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"candidate": self.candidate.to_dict(), "quantity": self.quantity,
                "verdicts": [v.to_dict() for v in self.verdicts], "score": self.score,
                "discount_pct": self.discount_pct, "warnings": list(self.warnings)}

    @classmethod
    def from_dict(cls, d: dict) -> "Ranked":
        return cls(candidate=Candidate.from_dict(d["candidate"]), quantity=d["quantity"],
                   verdicts=[Verdict.from_dict(v) for v in d["verdicts"]], score=d["score"],
                   discount_pct=d["discount_pct"], warnings=list(d["warnings"]))


@dataclass
class Excluded:
    product_id: str
    title: str
    reason: str   # hard_no:<word> | kids_21 | young_only | date | quantity

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Excluded":
        return cls(**d)


@dataclass
class RankResult:
    shortlist: list[Ranked]
    top: list[Ranked]
    excluded: list[Excluded]
    emptied_by: str | None
    adult_dropped: int = 0   # set by the /argue handler from the Scout result; rank() leaves 0
    away_dropped: int = 0    # likewise: products with no location in the plan's area
    partner_empty: bool = False   # likewise: the Partner search returned zero products
    blocking_words: list = field(default_factory=list)   # [{"word", "member_id"}] for every fully excluded product
    young_dropped: int = 0        # products left out because they are made for younger guests
    likes_matched_any: bool = True   # False when members have likes but no shortlisted option matched one

    def to_dict(self) -> dict:
        return {"shortlist": [r.to_dict() for r in self.shortlist],
                "top": [r.to_dict() for r in self.top],
                "excluded": [e.to_dict() for e in self.excluded],
                "emptied_by": self.emptied_by, "adult_dropped": self.adult_dropped,
                "away_dropped": self.away_dropped, "partner_empty": self.partner_empty,
                "blocking_words": [dict(b) for b in self.blocking_words], "young_dropped": self.young_dropped,
                "likes_matched_any": self.likes_matched_any}

    @classmethod
    def from_dict(cls, d: dict) -> "RankResult":
        return cls(shortlist=[Ranked.from_dict(r) for r in d["shortlist"]],
                   top=[Ranked.from_dict(r) for r in d["top"]],
                   excluded=[Excluded.from_dict(e) for e in d["excluded"]],
                   emptied_by=d["emptied_by"], adult_dropped=d.get("adult_dropped", 0),
                   away_dropped=d.get("away_dropped", 0), partner_empty=bool(d.get("partner_empty", False)),
                   blocking_words=[dict(b) for b in d.get("blocking_words", [])],
                   young_dropped=d.get("young_dropped", 0),
                   likes_matched_any=bool(d.get("likes_matched_any", True)))


_MONTH_DAY_RX = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\d)")
_CUE_RX = re.compile(r"\b(to|through|thru|until|valid|and|or)\b|[-&]", re.IGNORECASE)
_SECOND_DAY_RX = re.compile(r"\s*(?:,\s*\d{1,2}(?!\d)(?!\s*(?::|[ap]\.?m\b))|/)", re.IGNORECASE)
_MONTH_NUM = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
PAST_GRACE_DAYS = 60


def _month_day(text: str, today: date) -> tuple[bool, str | None]:
    """(text has a month-and-day, the ISO date when it names exactly one and nothing cues a range or choice)."""
    matches = list(_MONTH_DAY_RX.finditer(text or ""))
    if not matches:
        return False, None
    if len(matches) != 1 or _CUE_RX.search(text) or _SECOND_DAY_RX.match(text, matches[0].end()):
        return True, None
    month, day = _MONTH_NUM[matches[0].group(1)[:3].lower()], int(matches[0].group(2))
    if not 1 <= day <= 31:
        return True, None
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= today or (today - d).days <= PAST_GRACE_DAYS:
            return True, d.isoformat()
    return True, None


def fixed_date(text: str, today: date) -> str | None:
    """ISO date of the one event date named in the text, else None (ranges, choices and plain months are not dates)."""
    return _month_day(text, today)[1]


def parse_plan_date(text: str, today: date) -> str | None:
    return _month_day(text, today)[1]


def offer_fixed_date(c: Candidate, today: date) -> str | None:
    """The offer's fixed date: read from the option title, or from the title when the option names no month-day."""
    for text in (c.option_title, c.title):
        found, iso = _month_day(text, today)
        if found:
            return iso
    return None


def quantity_needed(group_size: int, party_size: int | None, bound: str | None,
                    min_units: int, title_party: int | None = None) -> int:
    group = max(group_size, 1)
    if bound == "exactly" and party_size and party_size > 0:
        qty = -(-group // party_size)
    elif title_party and not party_size:
        qty = -(-group // title_party)
    else:
        qty = group
    return max(qty, min_units or 1, 1)


def per_member_spend_minor(c: Candidate, qty: int, group_size: int) -> int:
    return -(-(qty * c.price_minor) // max(group_size, 1))


def _word_in(word: str, hay: list[str]) -> bool:
    if not word or not word.strip():
        return False
    rx = re.compile(r"(?<!\w)" + re.escape(word) + r"(?:s|es)?(?!\w)", re.IGNORECASE)
    return any(rx.search(h) for h in hay)


def _haystack(c: Candidate) -> list[str]:
    return [s for s in [c.title, c.option_title, *c.categories, *c.highlights] if isinstance(s, str)]


_YOUNG_RX = re.compile(r"\b(kids?|children|toddlers?)\b", re.IGNORECASE)
_YOUNG_OK_RX = re.compile(r"[- ]?(eat|eats|friendly|welcome|free|stay|stays)\b", re.IGNORECASE)


def made_for_young(c: Candidate) -> bool:
    """True when the option title or title names younger guests, unless every mention is an adult-friendly
    phrase ("kids eat free", "kid-friendly"). Categories are not read."""
    for text in (c.option_title, c.title):
        for m in _YOUNG_RX.finditer(text or ""):
            if not _YOUNG_OK_RX.match(text, m.end()):
                return True
    return False


def _discount_pct(c: Candidate) -> int | None:
    if c.original_minor and c.original_minor > c.price_minor > 0:
        return int(round((1 - c.price_minor / c.original_minor) * 100))
    return None


def _warnings(c: Candidate, any_kids: bool, fixed: str | None = None) -> list[str]:
    w: list[str] = []
    f = c.facts
    if f.limit_per_person is not None:
        w.append("limit_per_person")
    if c.title_party is None or c.party_size is not None:
        if c.party_bound in ("up_to", "at_least") or c.party_size is None:
            w.append("may_cover_more")
    if f.appointment is True:
        w.append("appointment")
    elif f.appointment is None:
        w.append("appointment_unknown")
    if any_kids and f.age_21 is None:
        w.append("age_unknown")
    if fixed:
        w.append("fixed_date:" + fixed)
    return w


def rank(cands: list[Candidate], members: list[Member], solo: bool = False,
         today: date | None = None, plan_date_text: str = "") -> RankResult:
    plan_iso = parse_plan_date(plan_date_text, today) if today else None
    group = max(len(members), 1)
    any_kids = any(m.kids for m in members)

    survivors: list[tuple[tuple, Ranked]] = []
    reasons: dict[str, str] = {}       # product_id -> first exclusion reason
    blocking: dict[str, list[dict]] = {}   # product_id -> every hard-no word that hit any of its options
    titles: dict[str, str] = {}
    for c in cands:
        hay = _haystack(c)
        qty = quantity_needed(len(members), c.party_size, c.party_bound, c.min_units, c.title_party)
        reason = None
        hits = [{"word": w, "member_id": m.id} for m in members for w in m.hard_nos
                if w.strip() and _word_in(w, hay)]
        if hits:
            reason = "hard_no:" + hits[0]["word"]
            seen = blocking.setdefault(c.product_id, [])
            for b in hits:
                if not any(b["member_id"] == x["member_id"] and b["word"].casefold() == x["word"].casefold()
                           for x in seen):
                    seen.append(b)
        if reason is None and any_kids and c.facts.age_21 is True:
            reason = "kids_21"
        if reason is None and not any_kids and made_for_young(c):
            reason = "young_only"
        fixed = offer_fixed_date(c, today) if today else None
        if reason is None and fixed and (fixed < today.isoformat() or (plan_iso and fixed != plan_iso)):
            reason = "date"
        if reason is None and c.max_units is not None and c.max_units < qty:
            reason = "quantity"
        if reason is None and solo and qty > 1:
            reason = "quantity"             # a solo plan buys exactly one voucher: a higher minimum never reaches the debate
        if reason is not None:
            reasons.setdefault(c.product_id, reason)
            titles.setdefault(c.product_id, c.title)
            continue

        spend = per_member_spend_minor(c, qty, group)
        verdicts = []
        for m in members:
            liked = [l for l in m.likes if _word_in(l, hay)]
            verdicts.append(Verdict(m.id, spend <= m.max_budget_minor,
                                    max(0, spend - m.max_budget_minor), liked))
        disc = _discount_pct(c)
        score = (10 * sum(v.fits_budget for v in verdicts)
                 + 2 * sum(len(v.likes_matched) for v in verdicts)
                 + 0.05 * (disc or 0))
        r = Ranked(c, qty, verdicts, score, disc, _warnings(c, any_kids, fixed if not plan_iso else None))
        survivors.append(((-score, spend, c.product_id, c.option_id), r))

    survivors.sort(key=lambda t: t[0])
    seen: set[str] = set()
    best: list[Ranked] = []
    for _, r in survivors:             # best option per product, in rank order
        if r.candidate.product_id not in seen:
            seen.add(r.candidate.product_id)
            best.append(r)
    surviving_pids = {r.candidate.product_id for _, r in survivors}
    excluded = [Excluded(pid, titles[pid], rs) for pid, rs in reasons.items()
                if pid not in surviving_pids]

    # `top` comes from every survivor that fits each member's budget, not just the first SHORTLIST_N: a
    # fitting option always outscores a non-fitting one of the same product (10 per fit beats the 5-point
    # discount term), so `best` already holds its best option. Top items missing from the cut are appended.
    top = [r for r in best if all(v.fits_budget for v in r.verdicts)][:TOP_N]
    shortlist = best[:SHORTLIST_N]
    listed = {id(r) for r in shortlist}
    shortlist += [r for r in top if id(r) not in listed]
    emptied_by = None
    if not survivors:
        if excluded:
            emptied_by = Counter(e.reason for e in excluded).most_common(1)[0][0]
    elif not top:
        emptied_by = "budget"
    out_pids = {e.product_id for e in excluded}
    blocking_words: list[dict] = []
    for pid in out_pids:
        for b in blocking.get(pid, []):
            if not any(b["member_id"] == x["member_id"] and b["word"].casefold() == x["word"].casefold()
                       for x in blocking_words):
                blocking_words.append(b)
    young_dropped = sum(1 for e in excluded if e.reason == "young_only")
    has_likes = any(l.strip() for m in members for l in m.likes)
    likes_matched_any = (not has_likes or not shortlist
                         or any(v.likes_matched for r in shortlist for v in r.verdicts))
    return RankResult(shortlist, top, excluded, emptied_by, blocking_words=blocking_words,
                      young_dropped=young_dropped, likes_matched_any=likes_matched_any)
