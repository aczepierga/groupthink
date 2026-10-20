"""Scripted debate: code decides, templates talk. No LLM, no randomness beyond the seed.

Everything a line says is read from the RankResult (Ranked, Verdict, Candidate): titles come from the
candidate, money from money.fmt, and the strongest fact per member is picked by fixed priority. A
member who did not tick "say my numbers" only ever gets templates without {over} / {cap}.

Rounds (max 3, max len(members) + 2 lines each):
  1. Scout: one line listing the shortlist, one line with the skip count (counts only).
  2. Each advocate speaks once about the option holding its member's strongest fact.
  3. Each advocate speaks once more (next strongest fact, a different option when possible), then the
     referee closes: the top pick, or the constraint that emptied the list plus the relax offer.
With an empty shortlist the debate is the scout round plus a referee round.
"""
import random
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from . import lines as L
from .fit import Member, Ranked, RankResult
from .money import fmt

FACT_PRIORITY = ["over_budget", "limit_per_person", "appointment", "may_cover_more",
                 "matches_like", "best_discount", "voucher_soon"]
SOON_DAYS = 60
DISCOUNT_MIN = 10          # a markdown under 10% is not worth arguing about
MAX_ROUNDS = 3
MEMBER_SPECIFIC = {"over_budget", "matches_like"}   # every other code is a fact about the offer itself
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Line:
    speaker: str
    kind: Literal["scout", "advocate", "referee"]
    text: str
    product_id: str | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Line":
        return cls(**d)


def _clean(s: str) -> str:
    return " ".join((s or "").split())


def _parse_date(s) -> date | None:
    if not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def _best_discount_idx(shortlist: list[Ranked]) -> int | None:
    best = None
    for i, r in enumerate(shortlist):
        if r.discount_pct is None or r.discount_pct < DISCOUNT_MIN:
            continue
        if best is None or r.discount_pct > shortlist[best].discount_pct:
            best = i
    return best


def member_facts(member: Member, r: Ranked, idx: int, best_discount_idx: int | None,
                 today: date) -> list[str]:
    """Fact codes that hold for this member on this shortlist option, in priority order."""
    verdict = next((v for v in r.verdicts if v.member_id == member.id), None)
    if verdict is None:
        return []
    f = r.candidate.facts
    held: set[str] = set()
    if not verdict.fits_budget:
        held.add("over_budget")
    if f.limit_per_person is not None and r.quantity > f.limit_per_person:
        held.add("limit_per_person")
    if f.appointment is True:
        held.add("appointment")
    if "may_cover_more" in r.warnings:
        held.add("may_cover_more")
    if verdict.likes_matched:
        held.add("matches_like")
    if best_discount_idx is not None and idx == best_discount_idx:
        held.add("best_discount")
    exp = _parse_date(r.candidate.voucher_expires)
    if exp is not None and 0 <= (exp - today).days <= SOON_DAYS:
        held.add("voucher_soon")
    return [c for c in FACT_PRIORITY if c in held]


def _pick(rng: random.Random, pool: list[str], used: set[str], numbers_ok: bool = True) -> str:
    eligible = [t for t in pool if numbers_ok or ("{over}" not in t and "{cap}" not in t)]
    fresh = [t for t in eligible if t not in used] or eligible
    t = rng.choice(fresh)
    used.add(t)
    return t


def _expires(s: str | None) -> str:
    d = _parse_date(s)
    return f"{_MONTHS[d.month - 1]} {d.day}" if d else ""


def _advocate_text(rng, used, member: Member, idx: int, speaker: str, r: Ranked, code: str) -> str:
    c = r.candidate
    verdict = next((v for v in r.verdicts if v.member_id == member.id), None)
    over = verdict.over_by_minor if verdict else 0
    tmpl = _pick(rng, L.ADVOCATE[code][L.PERSONALITIES[idx % 4]], used, member.share_numbers)
    return tmpl.format(
        name=speaker, title=_clean(c.title), over=fmt(over, c.precision),
        cap=fmt(member.max_budget_minor, c.precision), limit=c.facts.limit_per_person or "",
        qty=r.quantity, expires=_expires(c.voucher_expires), pct=r.discount_pct or "")


def _scout_lines(rng, used, result: RankResult) -> list[tuple[Line, str | None]]:
    out: list[tuple[Line, str | None]] = []
    sl = result.shortlist
    if not sl:
        t = _pick(rng, L.SCOUT_NONE, used)
    else:
        titles = "; ".join(f"{i + 1}) {_clean(r.candidate.title)}" for i, r in enumerate(sl))
        pool = L.SCOUT_ONE if len(sl) == 1 else L.SCOUT_MANY
        t = _pick(rng, pool, used).format(count=len(sl), titles=titles)
    out.append((Line("Scout", "scout", t, None), "scout_list"))
    skipped = len(result.excluded) + max(0, result.adult_dropped)
    pool = L.SKIP_ZERO if skipped == 0 else (L.SKIP_ONE if skipped == 1 else L.SKIP_MANY)
    out.append((Line("Scout", "scout", _pick(rng, pool, used).format(skipped=skipped), None),
                "scout_skip"))
    return out


def _referee_line(rng, used, result: RankResult) -> tuple[Line, str]:
    if result.top:
        top = result.top[0]
        pool = L.REFEREE_PICK_ONE if len(result.top) == 1 else L.REFEREE_PICK_MANY
        t = _pick(rng, pool, used).format(title=_clean(top.candidate.title), count=len(result.top))
        return Line("Referee", "referee", t, top.candidate.product_id), "referee_pick"
    reason = result.emptied_by
    if reason is None and not result.shortlist:
        pool, code = L.REFEREE_NONE, "referee_none"
    else:
        if reason == "budget":
            key = "budget"
        elif reason and reason.startswith("hard_no"):
            key = "hard_no"
        elif reason in ("kids_21", "quantity"):
            key = reason
        else:
            key = "other"
        pool, code = L.REFEREE_EMPTY[key], f"referee_{key}"
    return Line("Referee", "referee", _pick(rng, pool, used), None), code


def _choose(member: Member, shortlist: list[Ranked], facts_by_option: dict[int, list[str]],
            taken: list[tuple[int, str]], first: bool,
            said_shared: set[tuple[int, str]]) -> tuple[int, str] | None:
    """Pick (shortlist index, code) for this member. Skips pairs this member already said, prefers a
    fact about the offer that no one has stated yet, then a different option than before."""
    said = {i for i, _ in taken}
    options = [(prio, i, c) for i, codes in facts_by_option.items() for c in codes
               for prio in [FACT_PRIORITY.index(c)] if (i, c) not in taken]
    if options:
        options.sort(key=lambda t: ((t[1], t[2]) in said_shared, t[1] in said, t[0], t[1]))
        _, i, c = options[0]
        return i, c
    # nothing stands out: a plain "this fits me" line about an option not yet cited
    for i, r in enumerate(shortlist):
        v = next((v for v in r.verdicts if v.member_id == member.id), None)
        if v is not None and v.fits_budget and (i, "fits_ok") not in taken and i not in said:
            return i, "fits_ok"
    if first:
        for i, r in enumerate(shortlist):
            v = next((v for v in r.verdicts if v.member_id == member.id), None)
            if v is not None and v.fits_budget:
                return i, "fits_ok"
    return None


def run_debate_traced(result: RankResult, members: list[Member], seed: int,
                      today: date | None = None) -> list[list[tuple[Line, str | None]]]:
    """Rounds of (Line, fact code or scout/referee tag). Deterministic for (result, members, seed, today)."""
    today = today or date.today()
    rng = random.Random(seed)
    used: set[str] = set()
    rounds: list[list[tuple[Line, str | None]]] = [_scout_lines(rng, used, result)]
    sl = result.shortlist
    n = len(members)
    if sl and n:
        best_idx = _best_discount_idx(sl)
        facts = {m.id: {i: member_facts(m, r, i, best_idx, today) for i, r in enumerate(sl)}
                 for m in members}
        taken: dict[str, list[tuple[int, str]]] = {m.id: [] for m in members}
        said_shared: set[tuple[int, str]] = set()
        for rnd in (0, 1):
            order = [(k, members[k]) for k in range(n)]
            shift = (seed + rnd) % n
            order = order[shift:] + order[:shift]
            lines: list[tuple[Line, str | None]] = []
            for idx, m in order:
                pick = _choose(m, sl, facts[m.id], taken[m.id], (rnd == 0), said_shared)
                if pick is None:
                    continue
                i, code = pick
                taken[m.id].append(pick)
                if code not in MEMBER_SPECIFIC:
                    said_shared.add(pick)
                speaker = _clean(m.name) or f"Guest {idx + 1}"
                text = _advocate_text(rng, used, m, idx, speaker, sl[i], code)
                lines.append((Line(speaker, "advocate", text, sl[i].candidate.product_id), code))
            rounds.append(lines)
        rounds = [r for r in rounds if r]
    rounds_out = rounds[:MAX_ROUNDS]
    ref = _referee_line(rng, used, result)
    if len(rounds_out) < MAX_ROUNDS:
        rounds_out.append([ref])
    else:
        rounds_out[-1].append(ref)
    return rounds_out


def run_debate_rounds(result: RankResult, members: list[Member], seed: int,
                      today: date | None = None) -> list[list[Line]]:
    return [[ln for ln, _ in rnd] for rnd in run_debate_traced(result, members, seed, today)]


def run_debate(result: RankResult, members: list[Member], seed: int,
               today: date | None = None) -> list[Line]:
    return [ln for rnd in run_debate_rounds(result, members, seed, today) for ln in rnd]
