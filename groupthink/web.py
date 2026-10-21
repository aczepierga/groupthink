"""The Argue flow: plan, private cards, scripted debate, votes. Registered onto the one FastAPI app.

Identity: the host token and each member id live only in HttpOnly cookies (host_<plan>, m_<plan>); the
plan stores a SHA-256 of the host token. Nothing private is ever put in a URL. Every value that came
from a member or from the API goes through Jinja autoescape; nothing here is marked safe.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from . import demo as demo_data
from . import limits
from .candidates import Candidate
from .debate import run_debate
from .fit import Member, Ranked, RankResult, per_member_spend_minor, rank
from .kv import Kv
from .ls_client import LsClient
from .money import dollars_to_minor, fmt
from .scout import NEARBY, THIN_BELOW, scout
from .store import TTL_SECONDS, PlanNotFound, SqliteStore, hash_ip
from .store_durable import DurableStore

log = logging.getLogger("groupthink.web")

CITIES = ["Chicago", "Austin", "New York"]
MAX_RUNS = 2
MAX_BODY = 32 * 1024
MIN_GROUP, MAX_GROUP = 2, 6
DATE_MAX, THEME_MAX, CITY_MAX = 60, 80, 60
PURGE_EVERY = timedelta(hours=1)
SCOUT_PATH = "A"      # strict agent enrichment (fine print) with Partner price, place and ids

router = APIRouter()


# ---- wiring --------------------------------------------------------------------------------------

def default_store():
    """DurableStore when Upstash env is present, else SqliteStore at a temp path (local dev)."""
    kv = Kv.from_env()
    if kv is not None:
        return DurableStore(kv)
    return SqliteStore(os.path.join(tempfile.gettempdir(), "groupthink-dev.db"))


class Deps:
    """Store, LsClient and clock for the routes; the first two are built on first use."""

    def __init__(self, store=None, ls_client=None, now=None):
        self._store, self._ls = store, ls_client
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._last_purge = None

    @property
    def store(self):
        with self._lock:
            if self._store is None:
                self._store = default_store()
            return self._store

    @property
    def ls(self):
        with self._lock:
            if self._ls is None:
                self._ls = LsClient()
            return self._ls

    def purge_due(self, now) -> bool:
        with self._lock:
            if self._last_purge is None or now - self._last_purge >= PURGE_EVERY:
                self._last_purge = now
                return True
            return False


def get_deps(request: Request) -> Deps:
    return request.app.state.deps


def register(app, templates, deps: Deps):
    app.state.deps = deps
    app.state.templates = templates
    app.add_exception_handler(PageError, _page_error)
    app.add_exception_handler(PlanNotFound, _plan_gone)
    app.middleware("http")(_housekeeping)
    app.include_router(router)


# ---- errors and rendering --------------------------------------------------------------------------

class PageError(Exception):
    def __init__(self, status, template="error.html", **ctx):
        self.status, self.template, self.ctx = status, template, ctx


def gone():
    return PageError(410, "expired.html")


def refuse(status, message, title="Not allowed"):
    return PageError(status, "error.html", title=title, message=message)


def render(request, name, status=200, **ctx):
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status)


async def _page_error(request, exc: PageError):
    return render(request, exc.template, exc.status, **exc.ctx)


async def _plan_gone(request, exc):
    return render(request, "expired.html", 410)


SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' https: data:; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def cross_site(request: Request) -> bool:
    """A POST that the browser says came from another site: Sec-Fetch-Site other than same-origin / none,
    or an Origin whose host differs from the host this request was sent to."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host", "")
        if urlsplit(origin).netloc.lower() != host.lower():
            return True
    return False


async def _housekeeping(request, call_next):
    deps = request.app.state.deps
    path = request.url.path
    if request.method == "POST" and cross_site(request):
        response = render(request, "error.html", 403, title="Not allowed",
                          message="That request came from another site, so it was refused.")
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response
    if not path.startswith("/static/"):
        now = deps.now()
        if deps.purge_due(now):
            try:
                await run_in_threadpool(deps.store.purge_expired, now)
            except Exception as exc:   # housekeeping must never break a request
                log.warning("purge failed: %s", type(exc).__name__)
    response = await call_next(request)
    if not path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    for k, v in SECURITY_HEADERS.items():
        response.headers[k] = v
    return response


# ---- request helpers -------------------------------------------------------------------------------

def client_ip(request: Request) -> str:
    """x-real-ip, else the first x-forwarded-for entry, else the socket peer (spike ruling)."""
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


async def read_form(request: Request) -> dict:
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise refuse(413, "That form is too large.", "Too large")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise refuse(413, "That form is too large.", "Too large")
    try:
        parsed = parse_qs(body.decode("utf8", "replace"), keep_blank_values=True, max_num_fields=60)
    except ValueError:
        raise refuse(400, "That form could not be read.", "Bad request")
    return {k: v[0] for k, v in parsed.items()}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def set_cookie(response, name, value):
    response.set_cookie(name, value, max_age=TTL_SECONDS, path="/", secure=True, httponly=True,
                        samesite="lax")


def load_plan(deps, plan_id) -> dict:
    plan = deps.store.get_plan(plan_id)
    if not plan or plan.get("expired"):
        raise gone()
    return plan


def is_host(request, plan_id, plan) -> bool:
    token = request.cookies.get("host_" + plan_id)
    return bool(token) and hmac.compare_digest(_sha(token).encode(), str(plan.get("host_token_hash", "")).encode())


def require_host(request, plan_id, plan):
    if not is_host(request, plan_id, plan):
        raise refuse(403, "Only the host of this plan can do that, from the browser that created it.")


def current_member(request, plan_id, cards):
    mid = request.cookies.get("m_" + plan_id)
    return next((m for m in cards if mid and m.id == mid), None)


def share_url(request, plan_id) -> str:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host", "")
    return "%s://%s/p/%s" % (proto, host, plan_id)


def parse_budget(text) -> int | None:
    t = (text or "").strip().lstrip("$").replace(",", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 1 or d > 100000:
        return None
    return dollars_to_minor(float(d))


def split_words(text) -> list[str]:
    out = []
    for chunk in (text or "").replace("\n", ",").replace(";", ",").split(","):
        if chunk.strip():
            out.append(chunk.strip())
    return out


def _has_word(member: Member, word: str) -> bool:
    return any(h.strip().casefold() == word.strip().casefold() for h in member.hard_nos)


# ---- home and plans --------------------------------------------------------------------------------

def _plan_form_values(src) -> dict:
    return {"city": src.get("city", "Chicago"), "other_city": src.get("other_city", ""),
            "date_text": src.get("date_text", ""), "group_size": src.get("group_size", "3"),
            "theme": src.get("theme", "")}


@router.get("/")
def home(request: Request):
    values = _plan_form_values({})
    wanted = (request.query_params.get("city") or "").strip()[:CITY_MAX]
    if wanted in CITIES:
        values["city"] = wanted
    elif wanted:
        values["city"], values["other_city"] = "other", wanted
    return render(request, "new_plan.html", cities=CITIES, values=values, error=None)


def _admit(request: Request, deps: Deps):
    """Plan-creation gate shared by /plans and /demo: (ip_hash, now, None) when one more plan may be
    created, else (None, None, the 503 or 429 response)."""
    try:
        ip_hash = hash_ip(client_ip(request))
    except RuntimeError:
        log.error("IP_SALT is not configured")
        return None, None, render(request, "error.html", 503, title="Not ready",
                                  message="This site is not fully configured yet. Please try again later.")
    now = deps.now()
    ok, _ = limits.allow(deps.store, ip_hash, now)
    if not ok:
        return None, None, render(request, "limit.html", 429, kind="plans")
    return ip_hash, now, None


@router.post("/plans")
def create_plan(request: Request, form: dict = Depends(read_form), deps: Deps = Depends(get_deps)):
    ip_hash, now, refused = _admit(request, deps)
    if refused is not None:
        return refused
    values = _plan_form_values(form)

    def bad(message):
        return render(request, "new_plan.html", 400, cities=CITIES, values=values, error=message)

    city = form.get("city", "")
    if city == "other":
        city = form.get("other_city", "").strip()[:CITY_MAX]
        if not city:
            return bad("Type the city name.")
    elif city not in CITIES:
        return bad("Pick a city from the list.")
    date_text = form.get("date_text", "").strip()[:DATE_MAX]
    if not date_text:
        return bad("Say when you are planning to go, in your own words.")
    try:
        size = int(form.get("group_size", ""))
    except ValueError:
        size = 0
    if not MIN_GROUP <= size <= MAX_GROUP:
        return bad("Group size must be a number from %d to %d." % (MIN_GROUP, MAX_GROUP))
    token = secrets.token_urlsafe(24)
    plan_id = deps.store.create_plan({
        "city": city, "date_text": date_text, "group_size": size,
        "theme": form.get("theme", "").strip()[:THEME_MAX], "host_token_hash": _sha(token),
        "ip_hash": ip_hash, "created_at": now.isoformat()})
    response = RedirectResponse("/p/" + plan_id, status_code=303)
    set_cookie(response, "host_" + plan_id, token)
    return response


@router.post("/demo")
def create_demo(request: Request, form: dict = Depends(read_form), deps: Deps = Depends(get_deps)):
    """A plan for one visitor with two labelled sample people, so a single browser sees a full argument.
    Same CSRF check, rate limits and TTL as /plans; the offers it argues over are the live ones."""
    ip_hash, now, refused = _admit(request, deps)
    if refused is not None:
        return refused
    city = form.get("city", "")
    if city not in CITIES:
        return render(request, "error.html", 400, title="Bad request", message="Pick a city from the list.")
    token, member_id = secrets.token_urlsafe(24), secrets.token_urlsafe(12)
    plan_id = deps.store.create_plan({
        "city": city, "date_text": demo_data.DATE_TEXT, "group_size": demo_data.GROUP_SIZE,
        "theme": demo_data.THEME, "host_token_hash": _sha(token), "ip_hash": ip_hash,
        "created_at": now.isoformat(), "demo": True})
    people = [(member_id, demo_data.YOU)] + [(secrets.token_urlsafe(12), s) for s in demo_data.SAMPLES]
    for mid, p in people:
        deps.store.put_card(plan_id, Member.clean(
            mid, p["name"], p["budget_minor"], p["likes"], p["hard_nos"], p["kids"], p["constraints"],
            p["share_numbers"]), cap=demo_data.GROUP_SIZE)
    response = RedirectResponse("/p/" + plan_id, status_code=303)
    set_cookie(response, "host_" + plan_id, token)
    set_cookie(response, "m_" + plan_id, member_id)
    return response


@router.get("/p/{plan_id}")
def plan_page(plan_id: str, request: Request, deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    cards = deps.store.cards(plan_id)
    me = current_member(request, plan_id, cards)
    runs = deps.store.argue_count(plan_id)
    return render(request, "plan.html", plan_id=plan_id, plan=plan, names=[m.name for m in cards],
                  cards_in=len(cards), me=me, full=len(cards) >= plan["group_size"],
                  host=is_host(request, plan_id, plan), runs=runs, max_runs=MAX_RUNS,
                  share_url=share_url(request, plan_id))


# ---- cards -----------------------------------------------------------------------------------------

def _card_values(member: Member | None) -> dict:
    if member is None:
        return {"name": "", "budget": "", "likes": "", "hard_nos": "", "constraints": "", "kids": False,
                "share_numbers": False}
    return {"name": member.name, "budget": fmt(member.max_budget_minor)[1:], "likes": ", ".join(member.likes),
            "hard_nos": ", ".join(member.hard_nos), "constraints": member.constraints, "kids": member.kids,
            "share_numbers": member.share_numbers}


def _relax_offer(store, plan_id, member):
    """The hard-no word that emptied the last run, when this member is the one who set it."""
    got = store.get_result(plan_id)
    if member is None or got is None:
        return None
    result = got[1]
    reason = result.emptied_by or ""
    if not result.top and reason.startswith("hard_no:"):
        word = reason.split(":", 1)[1]
        return word if _has_word(member, word) else None
    return None


def _card_page(request, deps, plan_id, plan, cards, status=200, values=None, error=None, lost_seat=False):
    me = current_member(request, plan_id, cards)
    full = me is None and (lost_seat or len(cards) >= plan["group_size"])
    return render(request, "card.html", status if not full else 409, plan_id=plan_id, plan=plan, me=me,
                  full=full, values=values or _card_values(me), error=error,
                  saved=request.query_params.get("saved") == "1",
                  relax_word=_relax_offer(deps.store, plan_id, me))


@router.get("/p/{plan_id}/card")
def card_page(plan_id: str, request: Request, deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    return _card_page(request, deps, plan_id, plan, deps.store.cards(plan_id))


@router.post("/p/{plan_id}/card")
def save_card(plan_id: str, request: Request, form: dict = Depends(read_form),
              deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    cards = deps.store.cards(plan_id)
    me = current_member(request, plan_id, cards)
    if me is None and len(cards) >= plan["group_size"]:
        return _card_page(request, deps, plan_id, plan, cards)
    values = {"name": form.get("name", ""), "budget": form.get("budget", ""), "likes": form.get("likes", ""),
              "hard_nos": form.get("hard_nos", ""), "constraints": form.get("constraints", ""),
              "kids": bool(form.get("kids")), "share_numbers": bool(form.get("share_numbers"))}
    budget = parse_budget(values["budget"])
    if not values["name"].strip():
        return _card_page(request, deps, plan_id, plan, cards, 400, values, "Add your name.")
    if budget is None:
        return _card_page(request, deps, plan_id, plan, cards, 400, values,
                          "Budget must be a dollar amount from 1 to 100000.")
    member_id = me.id if me else secrets.token_urlsafe(12)
    if not deps.store.put_card(plan_id, Member.clean(
            member_id, values["name"], budget, split_words(values["likes"]), split_words(values["hard_nos"]),
            values["kids"], values["constraints"], values["share_numbers"]), cap=plan["group_size"]):
        return _card_page(request, deps, plan_id, plan, deps.store.cards(plan_id), lost_seat=True)
    response = RedirectResponse("/p/%s/card?saved=1" % plan_id, status_code=303)
    if me is None:
        set_cookie(response, "m_" + plan_id, member_id)
    return response


# ---- argue -----------------------------------------------------------------------------------------

def _seed(plan_id, runs) -> int:
    return int(_sha("%s:%d" % (plan_id, runs))[:8], 16)


@router.post("/p/{plan_id}/argue")
def run_argue(plan_id: str, request: Request, deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    require_host(request, plan_id, plan)
    store = deps.store
    members = store.cards(plan_id)
    if not members:
        raise refuse(400, "Fill in at least one card first, then start the argument.", "No cards yet")
    # Claim the run BEFORE the slow scout: a double click or a replayed POST cannot spend a third run.
    run = store.claim_run(plan_id, MAX_RUNS)
    if run is None:
        return render(request, "limit.html", 429, kind="runs")
    try:
        found = scout(deps.ls, plan["city"], plan.get("theme", ""), max(m.max_budget_minor for m in members),
                      len(members), SCOUT_PATH)
    except Exception as exc:   # LsApiError, a missing key, a network failure: say none of it
        store.release_run(plan_id)
        log.warning("scout failed: %s status=%s", type(exc).__name__, getattr(exc, "status", None))
        return render(request, "error.html", 502, title="Search is unavailable",
                      message="We could not reach LivingSocial just now. Nothing was changed and this did "
                              "not use up a run. Try again in a minute.")
    try:
        result = rank(found.candidates, members)
        result.adult_dropped = found.adult_dropped
        lines = run_debate(result, members, _seed(plan_id, run - 1), deps.now().date())
        store.save_result(plan_id, lines, result)
    except Exception:
        store.release_run(plan_id)
        raise
    return RedirectResponse("/p/%s/argue" % plan_id, status_code=303)


def tally(top: list[Ranked], votes: dict, pick: str | None, voters: int = 0) -> dict:
    """Vote counts over the current top options. Ties: the host's pick, else the highest score (top order).

    `final` is False while some of the `voters` have not voted yet and the host has not broken a tie:
    the page then says "Leading so far" instead of announcing a pick.
    """
    ids = [r.candidate.product_id for r in top]
    counts = {pid: int(votes.get(pid, 0)) for pid in ids}
    best = max(counts.values(), default=0)
    leaders = [pid for pid in ids if counts[pid] == best]
    winner = reason = None
    if best > 0 and len(leaders) == 1:
        winner, reason = leaders[0], "most votes"
    elif pick in leaders:
        winner, reason = pick, "the host broke the tie"
    elif best > 0:
        winner, reason = leaders[0], "tied on votes, highest score wins"
    final = winner is not None and (reason == "the host broke the tie" or sum(counts.values()) >= voters)
    return {"counts": counts, "best": best, "leaders": leaders, "winner": winner, "reason": reason,
            "final": final, "pickable": len(leaders) > 1 or best == 0}


def _date_text(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        return date.fromisoformat(v[:10]).isoformat()
    except ValueError:
        return v[:40]


def _warning_lines(r: Ranked) -> list[str]:
    c, out = r.candidate, []
    for code in r.warnings:
        if code == "limit_per_person":
            n = c.facts.limit_per_person
            line = "Fine print says limit %s per person." % n
            if n and r.quantity > n:
                line += " Your group needs %d, so this takes %d separate checkouts." % (r.quantity, -(-r.quantity // n))
            out.append(line)
        elif code == "may_cover_more":
            out.append("One voucher may already cover more than one person. Check the deal page before buying.")
        elif code == "appointment":
            out.append("Needs an appointment with the business after purchase.")
        elif code == "appointment_unknown":
            out.append("Appointment requirement: unknown.")
    return out


def _fact_lines(c: Candidate) -> list[str]:
    """Quiet facts about the offer, shown the same way whoever is in the group (never a warning)."""
    if c.facts.age_21 is None:
        return ["Age limit: not stated"]
    return ["21+ only"] if c.facts.age_21 else []


DEAL_HOSTS = ("livingsocial.com", "groupon.com")
_HOST_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*")
_BAD_URL_CHARS = re.compile(r"[\s\\\x00-\x1f\x7f]")
_BUY_SEGMENTS = {"buy", "cart", "checkout"}


def safe_deal_url(c: Candidate) -> str | None:
    """The public deal page for a result card, or None. Only a strict agent match carries one, and only
    an https URL on a livingsocial.com or groupon.com host (no credentials, no odd port, no buy or cart
    path) is ever linked. Anything else renders as no link at all."""
    url = c.offer_url
    if c.permalink is None or not isinstance(url, str) or not url or len(url) > 500:
        return None
    if _BAD_URL_CHARS.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return None
    if port not in (None, 443) or not _HOST_RE.fullmatch(host):
        return None
    if not any(host == d or host.endswith("." + d) for d in DEAL_HOSTS):
        return None
    if _BUY_SEGMENTS & {seg.lower() for seg in parts.path.split("/")}:
        return None
    return url


def _offer_view(r: Ranked, counts: dict) -> dict:
    c = r.candidate
    group = max(len(r.verdicts), 1)
    spend = per_member_spend_minor(c, r.quantity, group)
    why = ["Within every budget: about %s per person." % fmt(spend, c.precision)]
    liked = sum(1 for v in r.verdicts if v.likes_matched)
    if liked:
        why.append("Matches something %d of %d members like." % (liked, len(r.verdicts)))
    if r.discount_pct:
        why.append("%d%% off the original price." % r.discount_pct)
    if c.party_size is None or c.party_bound not in ("exactly", "up_to", "at_least"):
        party = "Voucher size: unknown."
    else:
        party = "Each voucher covers %s %d %s." % (
            {"exactly": "exactly", "up_to": "up to", "at_least": "at least"}[c.party_bound], c.party_size,
            "person" if c.party_size == 1 else "people")
    place = c.place or "unknown"
    n_votes = counts.get(c.product_id, 0)
    return {
        "product_id": c.product_id, "title": c.title, "option_title": c.option_title,
        "price": fmt(c.price_minor, c.precision),
        "original": fmt(c.original_minor, c.precision) if c.original_minor and c.original_minor > c.price_minor else None,
        "original_unknown": c.original_minor is None,
        "place": "Place: %s%s" % (place, " (%s)" % c.city if c.place and c.city else ""),
        "quantity": "Buy %d voucher%s for %d %s." % (r.quantity, "" if r.quantity == 1 else "s", group,
                                                     "person" if group == 1 else "people"),
        "party": party, "fine_print": c.fine_print, "why": why, "warnings": _warning_lines(r),
        "facts": _fact_lines(c), "deal_url": safe_deal_url(c),
        "promo": c.promo_code, "voucher_until": _date_text(c.voucher_expires),
        "votes": "%d vote%s" % (n_votes, "" if n_votes == 1 else "s"),
    }


def _cheapest(result: RankResult) -> dict | None:
    best = None
    for r in result.shortlist:
        spend = per_member_spend_minor(r.candidate, r.quantity, max(len(r.verdicts), 1))
        if best is None or spend < best[0]:
            best = (spend, r)
    if best is None:
        return None
    return {"title": best[1].candidate.title, "spend": fmt(best[0], best[1].candidate.precision)}


def _empty_view(result: RankResult) -> dict | None:
    if result.top:
        return None
    reason = result.emptied_by or ""
    if reason == "budget":
        return {"kind": "budget", "cheapest": _cheapest(result)}
    if reason.startswith("hard_no:"):
        return {"kind": "hard_no", "word": reason.split(":", 1)[1]}
    if reason in ("kids_21", "quantity"):
        return {"kind": reason}
    return {"kind": "none"}


def _thin_view(result: RankResult, city: str) -> dict | None:
    products = len(result.shortlist) + len(result.excluded)
    if products >= THIN_BELOW:
        return None
    if products == 0:
        note = "Live search found no offers in %s." % city
    else:
        note = "Live search found only %d offer%s in %s." % (products, "" if products == 1 else "s", city)
    return {"note": note, "nearby": NEARBY.get(city, [])}


@router.get("/p/{plan_id}/argue")
def argue_page(plan_id: str, request: Request, deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    got = deps.store.get_result(plan_id)
    if got is None:
        return RedirectResponse("/p/" + plan_id, status_code=303)
    lines, result = got
    cards = deps.store.cards(plan_id)
    tal = tally(result.top, deps.store.votes(plan_id), deps.store.get_pick(plan_id), len(cards))
    offers = [_offer_view(r, tal["counts"]) for r in result.top]
    winner = next((o for o in offers if o["product_id"] == tal["winner"]), None)
    host = is_host(request, plan_id, plan)
    me = current_member(request, plan_id, cards)
    empty = _empty_view(result)
    owns_hard_no = bool(empty and empty["kind"] == "hard_no" and me and _has_word(me, empty["word"]))
    return render(
        request, "argue.html", plan_id=plan_id, plan=plan,
        lines=[{"speaker": ln.speaker, "kind": ln.kind, "text": ln.text} for ln in lines],
        offers=offers, empty=empty, owns_hard_no=owns_hard_no, thin=_thin_view(result, plan["city"]),
        winner=winner, final=tal["final"], reason=tal["reason"], leaders=tal["leaders"], host=host,
        can_pick=host and tal["pickable"] and tal["best"] > 0 and len(tal["leaders"]) > 1,
        can_vote=me is not None,
        runs=deps.store.argue_count(plan_id), max_runs=MAX_RUNS)


# ---- relax, vote, pick -----------------------------------------------------------------------------

@router.post("/p/{plan_id}/relax")
def relax(plan_id: str, request: Request, form: dict = Depends(read_form), deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    store = deps.store
    drop = form.get("drop", "")
    runs_left = MAX_RUNS - store.argue_count(plan_id)
    if drop == "budget":
        require_host(request, plan_id, plan)
        got = store.get_result(plan_id)
        cheapest = _cheapest(got[1]) if got else None
        if cheapest is None:
            raise refuse(400, "There is no argument to relax yet.", "Nothing to relax")
        return render(request, "relax.html", 200, plan_id=plan_id, kind="budget", cheapest=cheapest,
                      runs_left=runs_left, host=True)
    if drop.startswith("hard_no:") and drop[8:].strip():
        word = drop[8:]
        cards = store.cards(plan_id)
        me = current_member(request, plan_id, cards)
        if me is None or not _has_word(me, word):
            raise refuse(403, "A hard no can only be dropped by the member who set it, from their own "
                              "browser. Ask them to open their card and confirm.")
        me.hard_nos = [h for h in me.hard_nos if h.strip().casefold() != word.strip().casefold()]
        store.put_card(plan_id, me)
        return render(request, "relax.html", 200, plan_id=plan_id, kind="hard_no", word=word.strip(),
                      runs_left=runs_left, host=is_host(request, plan_id, plan))
    raise refuse(400, "Nothing to relax there. Pick one of the options on the results page.", "Bad request")


@router.post("/p/{plan_id}/vote")
def vote(plan_id: str, request: Request, form: dict = Depends(read_form), deps: Deps = Depends(get_deps)):
    load_plan(deps, plan_id)
    store = deps.store
    me = current_member(request, plan_id, store.cards(plan_id))
    if me is None:
        raise refuse(403, "Only members with a card can vote. Open your card from the link you were sent.")
    got = store.get_result(plan_id)
    ids = {r.candidate.product_id for r in got[1].top} if got else set()
    if form.get("product_id") not in ids:
        raise refuse(400, "That is not one of the options on the table.", "Bad request")
    store.put_vote(plan_id, me.id, form["product_id"])
    return RedirectResponse("/p/%s/argue" % plan_id, status_code=303)


@router.post("/p/{plan_id}/pick")
def pick(plan_id: str, request: Request, form: dict = Depends(read_form), deps: Deps = Depends(get_deps)):
    plan = load_plan(deps, plan_id)
    require_host(request, plan_id, plan)
    store = deps.store
    got = store.get_result(plan_id)
    if got is None:
        raise refuse(400, "There is nothing to pick yet.", "Bad request")
    tal = tally(got[1].top, store.votes(plan_id), None)
    if not tal["pickable"] or form.get("product_id") not in tal["leaders"]:
        raise refuse(400, "The host only breaks ties, and only between the tied options.", "Bad request")
    store.set_pick(plan_id, form["product_id"])
    return RedirectResponse("/p/%s/argue" % plan_id, status_code=303)
