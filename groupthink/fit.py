"""Fit engine: deterministic ranking of candidates against a group. The debate only narrates this.

Hard filters (hard-no word, age 21 with kids, quantity over max_units) exclude. Budget is a score
term, not a filter: over-budget options stay in the shortlist as debate material but never in `top`.
Member text is untrusted: truncated, keyword-matched with escaped word-boundary patterns only.
"""
import re
from collections import Counter
from dataclasses import asdict, dataclass

from .candidates import Candidate

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
    reason: str   # hard_no:<word> | kids_21 | quantity

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

    def to_dict(self) -> dict:
        return {"shortlist": [r.to_dict() for r in self.shortlist],
                "top": [r.to_dict() for r in self.top],
                "excluded": [e.to_dict() for e in self.excluded],
                "emptied_by": self.emptied_by, "adult_dropped": self.adult_dropped}

    @classmethod
    def from_dict(cls, d: dict) -> "RankResult":
        return cls(shortlist=[Ranked.from_dict(r) for r in d["shortlist"]],
                   top=[Ranked.from_dict(r) for r in d["top"]],
                   excluded=[Excluded.from_dict(e) for e in d["excluded"]],
                   emptied_by=d["emptied_by"], adult_dropped=d.get("adult_dropped", 0))


def quantity_needed(group_size: int, party_size: int | None, bound: str | None,
                    min_units: int) -> int:
    group = max(group_size, 1)
    if bound == "exactly" and party_size and party_size > 0:
        qty = -(-group // party_size)
    else:
        qty = group
    return max(qty, min_units or 1, 1)


def per_member_spend_minor(c: Candidate, qty: int, group_size: int) -> int:
    return -(-(qty * c.price_minor) // max(group_size, 1))


def _word_in(word: str, hay: list[str]) -> bool:
    if not word or not word.strip():
        return False
    rx = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE)
    return any(rx.search(h) for h in hay)


def _haystack(c: Candidate) -> list[str]:
    return [s for s in [c.title, *c.categories, *c.highlights] if isinstance(s, str)]


def _discount_pct(c: Candidate) -> int | None:
    if c.original_minor and c.original_minor > c.price_minor > 0:
        return int(round((1 - c.price_minor / c.original_minor) * 100))
    return None


def _warnings(c: Candidate, any_kids: bool) -> list[str]:
    w: list[str] = []
    f = c.facts
    if f.limit_per_person is not None:
        w.append("limit_per_person")
    if c.party_bound in ("up_to", "at_least") or c.party_size is None:
        w.append("may_cover_more")
    if f.appointment is True:
        w.append("appointment")
    elif f.appointment is None:
        w.append("appointment_unknown")
    if any_kids and f.age_21 is None:
        w.append("age_unknown")
    return w


def rank(cands: list[Candidate], members: list[Member], solo: bool = False) -> RankResult:
    group = max(len(members), 1)
    any_kids = any(m.kids for m in members)
    hard_nos = [w for m in members for w in m.hard_nos if w.strip()]

    survivors: list[tuple[tuple, Ranked]] = []
    reasons: dict[str, str] = {}       # product_id -> first exclusion reason
    titles: dict[str, str] = {}
    for c in cands:
        hay = _haystack(c)
        qty = quantity_needed(len(members), c.party_size, c.party_bound, c.min_units)
        reason = None
        for w in hard_nos:
            if _word_in(w, hay):
                reason = f"hard_no:{w}"
                break
        if reason is None and any_kids and c.facts.age_21 is True:
            reason = "kids_21"
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
        r = Ranked(c, qty, verdicts, score, disc, _warnings(c, any_kids))
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
    return RankResult(shortlist, top, excluded, emptied_by)
