"""Candidates: one scored unit per sellable Partner option, enriched with agent fine print on path A.

Price, units, place and categories always come from the Partner product. Agent fields are added
only when `agent_details` holds an offer for the PARTNER product id (matching Partner product to
agent offer is Task 5's job; ids differ between the two APIs). Inside a matched product an agent
option is attached to a Partner option by PRICE equality, never by option id, and only when exactly
one agent option has that price.
"""
import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from .area import area_location
from .facts import Facts, extract_facts


@dataclass
class Candidate:
    product_id: str
    option_id: str
    permalink: str | None
    title: str
    option_title: str
    merchant: str | None
    place: str
    city: str
    price_minor: int
    original_minor: int | None
    precision: int
    party_size: int | None
    party_bound: str | None
    min_units: int
    max_units: int | None
    fine_print: list[str] | None
    highlights: list[str]
    categories: list[str]
    voucher_expires: str | None
    promo_code: str | None
    facts: Facts = field(default_factory=Facts)
    offer_url: str | None = None   # agent offerUrl (public deal page) on a strict match; validated at render
    title_party: int | None = None   # people one voucher covers, read from the option title or title

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        d = dict(d)
        d["facts"] = Facts(**(d.get("facts") or {}))
        return cls(**d)


_NUM_WORDS = {w: i for i, w in enumerate(
    "two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split(), start=2)}
_NUM = r"(\d{1,3}|" + "|".join(_NUM_WORDS) + r")(?!\d)"
_PEOPLE_RX = re.compile(r"(?<![\w$])(?:for|up to)\s+(?:up to\s+)?" + _NUM +
                        r"\s+(?:people|persons|guests|players|adults|riders|diners)\b", re.IGNORECASE)
_FOR_RX = re.compile(r"(?<![\w$])for\s+" + _NUM + r"(?=\s*(?:$|[(,)\-|]))", re.IGNORECASE)
_FOR_NUMBER_RX = re.compile(r"(?<!\w)for\s+\$?(?:\d|" + "|".join(_NUM_WORDS) + r")", re.IGNORECASE)
_MORE_SIZES_RX = re.compile(r"\s*(?:,\s*(?:or\s+|and\s+)?|\s+or\s+)(?:\d|" + "|".join(_NUM_WORDS) + r")\b",
                            re.IGNORECASE)


def _party_n(token: str) -> int | None:
    n = int(token) if token.isdigit() else _NUM_WORDS.get(token.lower())
    return n if n is not None and 2 <= n <= 20 else None


def _party_in(text: str) -> tuple[bool, int | None]:
    """(text names a people count, the count or None when it is ambiguous or out of range)."""
    sizes: set[int] = set()
    found = False
    for rx in (_PEOPLE_RX, _FOR_RX):
        for m in rx.finditer(text):
            n = _party_n(m.group(1))
            if n is None:
                continue
            found = True
            if _MORE_SIZES_RX.match(text, m.end()):
                return True, None
            sizes.add(n)
    return found, (next(iter(sizes)) if len(sizes) == 1 else None)


def title_party(option_title: str, title: str) -> int | None:
    """People one voucher covers, read from the option title first, then the title. A count that is not a
    people count (hours, months, a price, a list of sizes) reads as unknown, never as a guess."""
    found, n = _party_in(option_title or "")
    if found:
        return n
    if _FOR_NUMBER_RX.search(option_title or ""):
        return None
    return _party_in(title or "")[1]


def _minor(v) -> int | None:
    return None if v is None else int(round(v))


def _place_city(hit: dict, plan_city: str = "") -> tuple[str, str]:
    """Place and city shown: the first location in the plan's area (chains list far-away branches first);
    with no plan city, or none in the area, the first location."""
    locs = hit.get("locations") or []
    if not locs:
        return "", ""
    loc = (area_location(plan_city, hit) if plan_city else None) or locs[0]
    return (loc.get("name") or loc.get("street") or ""), (loc.get("city") or "")


def _agent_option_for(retail_minor: int, agent_options: list[dict]) -> dict | None:
    """The one agent option priced exactly retail_minor, else None (zero or ambiguous)."""
    same = [o for o in agent_options
            if isinstance(o.get("price"), dict) and o["price"].get("amountMinor") == retail_minor]
    return same[0] if len(same) == 1 else None


def build_candidates(partner_hits: list[dict], agent_details: dict[str, dict],
                     path: Literal["A", "B"], plan_city: str = "") -> list[Candidate]:
    out: list[Candidate] = []
    for hit in partner_hits:
        if hit.get("availabilityRequired") is not False:
            continue
        if hit.get("status", "active") != "active":
            continue
        pid = hit["id"]
        agent = agent_details.get(pid) if path == "A" else None
        place, city = _place_city(hit, plan_city)
        fine_print = agent.get("finePrint") if agent else None
        for opt in hit.get("options") or []:
            if opt.get("active") is False:   # null = unknown = sellable
                continue
            price = opt.get("price")
            if not isinstance(price, dict) or price.get("retail") is None:
                continue                      # no published price: cannot be scored or carted
            retail = _minor(price["retail"])
            a_opt = _agent_option_for(retail, agent.get("options") or []) if agent else None
            ps = (a_opt or {}).get("partySize") or {}
            people = ps.get("people")
            out.append(Candidate(
                product_id=pid,
                option_id=opt["id"],
                permalink=agent.get("permalink") if agent else None,
                title=hit.get("title") or "",
                option_title=opt.get("title") or "",
                merchant=agent.get("merchant") if agent else None,
                place=place,
                city=city,
                price_minor=retail,
                original_minor=_minor(price.get("original")),
                precision=int(price.get("currencyPrecision", 2)),
                party_size=int(people) if people is not None else None,
                party_bound=ps.get("bound"),
                min_units=int(opt.get("minUnits") or 1),
                max_units=int(opt["maxUnits"]) if opt.get("maxUnits") is not None else None,
                fine_print=fine_print,
                highlights=list(agent.get("highlights") or []) if agent else [],
                categories=list(hit.get("categoryLabels") or []),
                voucher_expires=(a_opt or {}).get("voucherExpires"),
                promo_code=(a_opt or {}).get("promoCode"),
                facts=extract_facts(fine_print),
                offer_url=agent["offerUrl"] if agent and isinstance(agent.get("offerUrl"), str) else None,
                title_party=title_party(opt.get("title") or "", hit.get("title") or ""),
            ))
    return out
