"""Scout: fetch live offers and turn them into Candidates for the fit engine.

Partner search is the source of price, place and categories. Path "A" (strict match, "A prime" in
the spike report) also asks the agent API for fine print: Partner and agent ids do not match, so a
Partner product is paired with an agent offer by normalized title plus city, then confirmed by
price (every active Partner option price must appear among the agent option prices). Anything that
fails a check stays Partner-only. Path "B" never calls the agent API.
"""
import logging
import re
import time
from dataclasses import dataclass

from .candidates import Candidate, build_candidates
from .ls_client import LsApiError

log = logging.getLogger("groupthink.scout")

DETAIL_CAP = 12            # agent per-offer reads per scout (keeps /argue under 15 s)
MAX_QUERIES = 4
MAX_PRODUCTS = 30
THIN_BELOW = 5
BUDGET_S = 12.0            # wall clock for one scout; Vercel cuts /argue off shortly after
READ_CUTOFF = 8 / 12       # share of the budget after which no new agent search or read starts
QUERY_CUTOFF = 10 / 12     # share of the budget after which no new Partner query starts
BASE_QUERIES = ["things to do", "dinner", "activities"]
ADULT_TERMS = ["adults only", "adult night", "adults night", "adult entertainment", "adult store",
               "adult shop", "adult toys", "thc", "cannabis", "dispensary", "strip club", "sex",
               "erotic", "hookah", "21+ night"]
NEARBY = {"Chicago": ["Evanston", "Oak Park"], "Austin": ["Round Rock"],
          "New York": ["Brooklyn", "Jersey City"]}

# Whole words only, with an optional plural "s". Bare "adult" is not a term ("Adult Pottery Class" is a
# legitimate grown-up class), and "cbd" is not a term (CBD massage deals are legitimate). "NY Strip
# Steak" and "Essex" are not caught: "strip club" needs both words, "sex" needs a word boundary.
_ADULT_RE = re.compile(r"\b(?:%s)s?\b" % "|".join(re.escape(t) for t in ADULT_TERMS), re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


@dataclass
class ScoutResult:
    candidates: list[Candidate]
    queries_tried: list[str]
    thin: bool
    adult_dropped: int = 0


def is_adult(hit: dict) -> bool:
    """Adult term as a whole word in the title or in any category label."""
    texts = [hit.get("title") or ""] + [str(c) for c in (hit.get("categoryLabels") or [])]
    return any(_ADULT_RE.search(t) for t in texts)


def normalize_title(s: str | None) -> str:
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub("", (s or "").casefold())).strip()


def _first_city(hit: dict) -> str:
    locs = hit.get("locations") or []
    return ((locs[0].get("city") or "") if locs else "").casefold().strip()


def match_agent(hit: dict, summaries: list[dict]) -> dict | None:
    """The one agent summary whose normalized title equals the Partner title and whose city equals the
    Partner hit's first location city. No match, or more than one distinct match, gives None."""
    title, city = normalize_title(hit.get("title")), _first_city(hit)
    if not title or not city:
        return None
    found = {}
    for s in summaries:
        if normalize_title(s.get("title")) == title and (s.get("city") or "").casefold().strip() == city:
            found[s.get("id") or s.get("permalink")] = s
    return next(iter(found.values())) if len(found) == 1 else None


def confirm_match(hit: dict, detail: dict) -> bool:
    """True when EVERY active, priced Partner option has an agent option at the same price (cents)."""
    agent_prices = {o["price"].get("amountMinor") for o in (detail.get("options") or [])
                    if isinstance(o, dict) and isinstance(o.get("price"), dict)}
    wanted = []
    for opt in hit.get("options") or []:
        if opt.get("active") is False:
            continue
        price = opt.get("price")
        if isinstance(price, dict) and price.get("retail") is not None:
            wanted.append(int(round(price["retail"])))
    return bool(wanted) and all(p in agent_prices for p in wanted)


def _queries(theme: str) -> list[str]:
    out, seen = [], set()
    for q in [(theme or "").strip()] + BASE_QUERIES:
        if q and q.casefold() not in seen:
            seen.add(q.casefold())
            out.append(q)
    return out[:MAX_QUERIES]


def _sellable(hit: dict) -> bool:
    return hit.get("availabilityRequired") is False and hit.get("status", "active") == "active"


def scout(client, city: str, theme: str, max_cap_minor: int, group_size: int, path: str,
          budget_s: float = BUDGET_S, clock=time.monotonic) -> ScoutResult:
    """Fetch and build candidates inside a wall-clock budget (default 12 s, i.e. 8 s for the last agent
    read to start and 10 s for the last query). Past a cutoff nothing new starts and what was gathered is
    returned: later offers simply stay Partner-only, and fewer queries just mean fewer products."""
    if path not in ("A", "B"):
        raise ValueError("path must be 'A' or 'B'")
    start = clock()
    elapsed = lambda: clock() - start                          # noqa: E731
    read_ok = lambda: elapsed() < budget_s * READ_CUTOFF       # noqa: E731
    query_ok = lambda: elapsed() < budget_s * QUERY_CUTOFF     # noqa: E731
    max_price = -(-(max_cap_minor * group_size) // 100)          # whole dollars, rounded up
    max_price = max_price if max_price > 0 else None

    tried, unique = [], {}
    for q in _queries(theme):
        if not query_ok():
            log.info("scout budget: no new query after %d tried", len(tried))
            break
        tried.append(q)
        try:
            page = client.search_partner(q, city, max_price_dollars=max_price)
        except LsApiError as e:
            if not unique:
                raise                                            # nothing gathered yet: caller decides
            log.info("partner search failed (%s) on query %d, keeping %d products", e.status,
                     len(tried), len(unique))
            break
        for h in page:
            unique.setdefault(h["id"], h)                        # insertion order = Partner rank
        if len(unique) >= MAX_PRODUCTS:
            break

    kept = [h for h in unique.values() if not is_adult(h)]
    adult_dropped = len(unique) - len(kept)

    agent_details: dict[str, dict] = {}
    if path == "A" and any(_sellable(h) for h in kept):
        summaries = _agent_summaries(client, tried, city, read_ok)
        fetched = 0
        for h in kept:
            if fetched >= DETAIL_CAP:
                break
            if not read_ok():
                log.info("scout budget: no new agent read after %d", fetched)
                break
            if not _sellable(h):
                continue
            s = match_agent(h, summaries)
            if s is None or not s.get("permalink"):
                continue
            fetched += 1
            try:
                detail = client.get_agent_offer(s["permalink"], city)
            except LsApiError as e:
                log.info("agent read failed (%s), product stays Partner-only", e.status)
                if e.status == 429:
                    break                                        # budget exhausted: rest stay Partner-only
                continue
            if isinstance(detail, dict) and confirm_match(h, detail):
                agent_details[h["id"]] = dict(detail, permalink=detail.get("permalink") or s["permalink"])

    candidates = build_candidates(kept, agent_details, path)
    thin = len({c.product_id for c in candidates}) < THIN_BELOW
    return ScoutResult(candidates=candidates, queries_tried=tried, thin=thin, adult_dropped=adult_dropped)


def _agent_summaries(client, queries: list[str], city: str, may_start=lambda: True) -> list[dict]:
    seen, out = set(), []
    for q in queries:
        if not may_start():
            break
        try:
            page = client.search_agent(q, city)
        except LsApiError as e:
            log.info("agent search failed (%s), continuing Partner-only for this query", e.status)
            continue
        for s in page:
            key = s.get("id") or s.get("permalink")
            if key not in seen:
                seen.add(key)
                out.append(s)
    return out
