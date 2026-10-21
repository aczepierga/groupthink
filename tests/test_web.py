"""Argue flow: FastAPI TestClient, fake LsClient, SqliteStore. No network, no real key."""
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from groupthink import limits
from groupthink import web
from groupthink.kv import KvError
from groupthink.ls_client import LsApiError, LsClient
from groupthink.scout import NEARBY
from groupthink.store import PlanNotFound, SqliteStore
from groupthink.web import client_ip
from groupthink.web_skeleton import create_app

REQUIRED_COPY = ("You're buying a voucher. It does not book a date or time - "
                 "most of these need an appointment with the business after purchase.")
KEY = "grpn" + "_SECRETVALUE"   # built at runtime so the source tree never holds a grpn_ literal
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)


# ---- fakes -------------------------------------------------------------------------------

def p_hit(pid, title, price, city="Chicago", cats=("Things To Do",), place="Kiln & Co", **kw):
    h = {"id": pid, "title": title, "status": "active", "availabilityRequired": False,
         "categoryLabels": list(cats),
         "locations": [{"id": "l1", "name": place, "city": city, "state": "Illinois"}],
         "options": [{"id": pid + "-o1", "title": "Standard", "active": True, "minUnits": 1,
                      "maxUnits": None,
                      "price": {"retail": price, "original": price * 2, "currency": "USD",
                                "currencyPrecision": 2}}]}
    h.update(kw)
    return h


def a_summary(pid, title, city="Chicago"):
    return {"id": "a-" + pid, "permalink": "pl-" + pid, "title": title, "city": city}


def a_detail(pid, price, fine=("Appointment required.", "Limit 1 per person."), expires="2026-12-31",
             promo="FALL10"):
    return {"id": "a-" + pid, "permalink": "pl-" + pid, "merchant": "Kiln & Co",
            "finePrint": list(fine) if fine is not None else None, "highlights": [],
            "options": [{"id": "ao1", "title": "A",
                         "price": {"amountMinor": price, "currency": "USD", "currencyExponent": 2},
                         "partySize": {"people": 1, "bound": "exactly"},
                         "voucherExpires": expires, "promoCode": promo}]}


class FakeLs:
    """Quacks like LsClient for the Scout. `agent=False` leaves every offer Partner-only."""

    def __init__(self, hits=None, agent=True, error=None, fine=("Appointment required.", "Limit 1 per person.")):
        self.hits = hits if hits is not None else [
            p_hit("p1", "Pottery Wheel Night", 3000),
            p_hit("p2", "Axe Throwing Hour", 2500),
            p_hit("p3", "Escape Room Hour", 2000)]
        self.agent, self.error, self.fine, self.calls = agent, error, fine, []

    def search_partner(self, q, city, max_price_dollars=None, limit=50):
        self.calls.append(("partner", q, city))
        if self.error:
            raise self.error
        return list(self.hits)

    def search_agent(self, q, city, limit=50):
        return [a_summary(h["id"], h["title"], city) for h in self.hits] if self.agent else []

    def get_agent_offer(self, permalink, city=None):
        pid = permalink[3:]
        h = next(x for x in self.hits if x["id"] == pid)
        return a_detail(pid, h["options"][0]["price"]["retail"], fine=self.fine)


class Env:
    def __init__(self, store=None, ls=None):
        self.clock = [T0]
        self.store = store or SqliteStore(":memory:", now=lambda: self.clock[0])
        self.ls = ls or FakeLs()
        self.app = create_app(store=self.store, ls_client=self.ls, now=lambda: self.clock[0])

    def browser(self):
        return TestClient(self.app, base_url="https://testserver")

    def advance(self, **kw):
        self.clock[0] += timedelta(**kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IP_SALT", "test-salt-not-secret")
    for name in ("PROBES_ENABLED", "KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
                 "UPSTASH_REDIS_REST_TOKEN", "LS_PARTNER_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env():
    return Env()


def plan_id_of(resp):
    m = re.fullmatch(r"/p/([A-Za-z0-9_-]+)", resp.headers["location"])
    assert m, resp.headers.get("location")
    return m.group(1)


def new_plan(c, ip="203.0.113.10", **over):
    form = {"city": "Chicago", "date_text": "Sat Nov 8", "group_size": "3", "theme": ""}
    form.update(over)
    r = c.post("/plans", data=form, headers={"x-real-ip": ip}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return plan_id_of(r)


def card(c, pid, name="Ann", budget="50", likes="", hard_nos="", constraints="", kids=False,
         share=False, expect=303):
    form = {"name": name, "budget": budget, "likes": likes, "hard_nos": hard_nos,
            "constraints": constraints}
    if kids:
        form["kids"] = "on"
    if share:
        form["share_numbers"] = "on"
    r = c.post(f"/p/{pid}/card", data=form, follow_redirects=False)
    assert r.status_code == expect, r.text
    return r


def argue(c, pid, expect=303):
    r = c.post(f"/p/{pid}/argue", follow_redirects=False)
    assert r.status_code == expect, r.text
    return r


def group(env, ls=None, host_budget="50", others=(("Bo", "40"), ("Cy", "60")), size="3", **host_kw):
    """Host Ann plus other members, each in its own browser. Returns (pid, host, [member browsers])."""
    host = env.browser()
    pid = new_plan(host, group_size=size)
    card(host, pid, "Ann", host_budget, **host_kw)
    members = []
    for name, budget in others:
        b = env.browser()
        card(b, pid, name, budget)
        members.append(b)
    return pid, host, members


def top_product_ids(env, pid):
    return [r.candidate.product_id for r in env.store.get_result(pid)[1].top]


# ---- plans, cookies, links ----------------------------------------------------------------

def test_home_form_and_required_copy(env):
    r = env.browser().get("/")
    assert r.status_code == 200
    assert REQUIRED_COPY in r.text
    for city in ("Chicago", "Austin", "New York", "other"):
        assert f'value="{city}"' in r.text
    assert 'name="group_size"' in r.text and 'name="date_text"' in r.text and 'name="theme"' in r.text


def test_create_plan_sets_host_cookie_and_redirects(env):
    c = env.browser()
    r = c.post("/plans", data={"city": "Austin", "date_text": "Friday", "group_size": "4", "theme": "tacos"},
               follow_redirects=False)
    pid = plan_id_of(r)
    sc = r.headers["set-cookie"].lower()
    assert f"host_{pid}=".lower() in sc
    assert "httponly" in sc and "secure" in sc and "samesite=lax" in sc
    assert env.store.get_plan(pid)["city"] == "Austin"
    assert "host_token_hash" in env.store.get_plan(pid)


@pytest.mark.parametrize("over", [{"group_size": "1"}, {"group_size": "7"}, {"group_size": "x"},
                                  {"city": "Atlantis"}, {"city": "other", "other_city": "  "},
                                  {"date_text": ""}])
def test_bad_plan_form_is_400(env, over):
    form = {"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""}
    form.update(over)
    r = env.browser().post("/plans", data=form, follow_redirects=False)
    assert r.status_code == 400
    assert REQUIRED_COPY in r.text   # the form is shown again


def test_other_city_uses_free_text(env):
    c = env.browser()
    pid = new_plan(c, city="other", other_city="Denver")
    assert env.store.get_plan(pid)["city"] == "Denver"


def test_host_token_not_in_url(env):
    c = env.browser()
    r = c.post("/plans", data={"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""},
               follow_redirects=False)
    pid = plan_id_of(r)
    token = c.cookies.get(f"host_{pid}")
    assert token and len(token) >= 20
    assert token not in r.headers["location"] and token not in r.text
    seen = [r]
    seen.append(c.get(f"/p/{pid}", follow_redirects=False))
    seen.append(card(c, pid))
    seen.append(c.get(f"/p/{pid}/card"))
    seen.append(argue(c, pid))
    seen.append(c.get(f"/p/{pid}/argue"))
    for resp in seen:
        assert token not in resp.headers.get("location", "")
        assert token not in resp.text
        assert not any(token in str(v) for k, v in resp.headers.items() if k.lower() != "set-cookie")
    stored = env.store.get_plan(pid)
    assert token not in str(stored)   # only a hash is stored


def test_member_cookie_flags_and_not_in_url(env):
    c = env.browser()
    pid = new_plan(c)
    r = card(c, pid)
    sc = r.headers["set-cookie"].lower()
    assert f"m_{pid}=".lower() in sc and "httponly" in sc and "secure" in sc and "samesite=lax" in sc
    mid = c.cookies.get(f"m_{pid}")
    assert mid not in r.headers["location"]
    assert mid not in c.get(f"/p/{pid}/card").text


def test_share_page_shows_link_not_host_token(env):
    c = env.browser()
    pid = new_plan(c)
    t = c.get(f"/p/{pid}").text
    assert f"https://testserver/p/{pid}" in t
    assert "Let them argue" not in t            # no card yet, nothing to argue about
    card(c, pid)
    assert "Let them argue" in c.get(f"/p/{pid}").text
    guest = env.browser().get(f"/p/{pid}").text
    assert "Let them argue" not in guest


# ---- rate limits ---------------------------------------------------------------------------

def test_rate_limit_21st_plan_429(env):
    c = env.browser()
    for _ in range(20):
        new_plan(c, ip="198.51.100.1")
    r = c.post("/plans", data={"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""},
               headers={"x-real-ip": "198.51.100.1"}, follow_redirects=False)
    assert r.status_code == 429
    assert "198.51.100.1" not in r.text
    new_plan(c, ip="198.51.100.2")        # another client is unaffected


def test_rate_limit_keys_on_real_ip_then_forwarded_for(env):
    c = env.browser()
    form = {"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""}
    for i in range(20):
        r = c.post("/plans", data=form, headers={"x-real-ip": "198.51.100.5", "x-forwarded-for": f"9.9.9.{i}"},
                   follow_redirects=False)
        assert r.status_code == 303
    r = c.post("/plans", data=form, headers={"x-real-ip": "198.51.100.5", "x-forwarded-for": "1.2.3.4"},
               follow_redirects=False)
    assert r.status_code == 429
    for i in range(20):
        c.post("/plans", data=form, headers={"x-forwarded-for": f"198.51.100.6, 10.0.0.{i}"},
               follow_redirects=False)
    r = c.post("/plans", data=form, headers={"x-forwarded-for": "198.51.100.6, 10.9.9.9"},
               follow_redirects=False)
    assert r.status_code == 429


def test_client_ip_order():
    def req(headers, client=("8.8.8.8", 1234)):
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                        "client": client})
    assert client_ip(req({"x-real-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2"})) == "1.1.1.1"
    assert client_ip(req({"x-forwarded-for": "2.2.2.2, 3.3.3.3"})) == "2.2.2.2"
    assert client_ip(req({})) == "8.8.8.8"
    assert client_ip(req({}, client=None)) == "unknown"


def test_daily_cap_429(env, monkeypatch):
    monkeypatch.setattr(limits, "PLANS_PER_DAY_ALL", 3)
    c = env.browser()
    for i in range(3):
        new_plan(c, ip=f"198.51.100.{i + 10}")
    r = c.post("/plans", data={"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""},
               headers={"x-real-ip": "198.51.100.99"}, follow_redirects=False)
    assert r.status_code == 429


# ---- cards: privacy and escaping ---------------------------------------------------------------

def test_card_text_escaped(env):
    c = env.browser()
    pid = new_plan(c)
    hostile = "<script>alert(1)</script>"
    card(c, pid, name=hostile, likes=hostile + ", <b>bold</b>", hard_nos="<i>x</i>", constraints=hostile)
    pages = [c.get(f"/p/{pid}/card").text, c.get(f"/p/{pid}").text]
    argue(c, pid)
    pages.append(c.get(f"/p/{pid}/argue").text)
    for t in pages:
        assert "<script>alert(1)" not in t
        assert "<b>bold</b>" not in t and "<i>x</i>" not in t
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[0]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[2]   # the name speaks in the debate


def test_card_text_truncated_and_odd_input(env):
    c = env.browser()
    pid = new_plan(c)
    card(c, pid, name="N" * 500, likes="a" * 2000, constraints="c" * 2000, hard_nos="")
    m = env.store.cards(pid)[0]
    assert len(m.name) <= 40 and len(m.constraints) <= 280 and all(len(x) <= 280 for x in m.likes)
    c2 = env.browser()
    pid2 = new_plan(c2)
    card(c2, pid2, name="Zoe \U0001F389", budget="12.50", likes="", hard_nos="")
    assert env.store.cards(pid2)[0].max_budget_minor == 1250
    argue(c2, pid2)                       # empty likes and emoji never break matching or the debate
    assert "Zoe \U0001F389" in c2.get(f"/p/{pid2}/argue").text


@pytest.mark.parametrize("budget", ["", "abc", "-5", "0", "1e99"])
def test_bad_budget_is_400(env, budget):
    c = env.browser()
    pid = new_plan(c)
    card(c, pid, budget=budget, expect=400)
    assert env.store.cards(pid) == []


def test_cards_not_visible_to_others(env):
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "53.47", likes="pottery zebra", hard_nos="mimes", constraints="secretnote ramp",
         kids=True, share=False)
    bo = env.browser()
    card(bo, pid, "Bo", "40")
    argue(host, pid)
    for browser in (bo, env.browser(), host):
        pages = [browser.get(f"/p/{pid}").text, browser.get(f"/p/{pid}/argue").text]
        for t in pages:
            for secret in ("zebra", "mimes", "secretnote", "53.47", "5347"):
                assert secret not in t, secret
    # Bo's own card page shows Bo's values, never Ann's
    bo_card = bo.get(f"/p/{pid}/card").text
    assert 'value="Bo"' in bo_card and "zebra" not in bo_card and "secretnote" not in bo_card
    # a visitor with no card sees a blank form
    blank = env.browser().get(f"/p/{pid}/card").text
    assert "zebra" not in blank and 'value="Ann"' not in blank


def test_full_plan_refuses_new_card(env):
    c = env.browser()
    pid = new_plan(c, group_size="2")
    card(c, pid, "Ann")
    card(env.browser(), pid, "Bo")
    r = card(env.browser(), pid, "Cy", expect=409)
    assert "full" in r.text.lower()
    assert [m.name for m in env.store.cards(pid)] == ["Ann", "Bo"]
    # a member can still edit their own card
    card(c, pid, "Ann", "65")
    by_name = {m.name: m for m in env.store.cards(pid)}
    assert by_name["Ann"].max_budget_minor == 6500 and sorted(by_name) == ["Ann", "Bo"]


# ---- argue ------------------------------------------------------------------------------------

def test_argue_requires_host_cookie_and_a_card(env):
    c = env.browser()
    pid = new_plan(c)
    r = argue(c, pid, expect=400)             # no card yet
    assert "card" in r.text.lower()
    card(c, pid)
    assert env.browser().post(f"/p/{pid}/argue", follow_redirects=False).status_code == 403
    assert env.store.argue_count(pid) == 0


def test_argue_flow_renders_lines_in_template_and_top_cards(env):
    pid, host, members = group(env, likes="pottery")
    r = argue(host, pid)
    assert r.headers["location"] == f"/p/{pid}/argue"
    t = members[0].get(f"/p/{pid}/argue").text
    m = re.search(r'<template id="debate-lines">(.*?)</template>', t, re.S)
    assert m and "Scout" in m.group(1) and "Referee" in m.group(1)
    assert m.group(1).count('<li class="line') == len(env.store.get_result(pid)[0])
    assert t.count('class="offer"') == 3
    assert REQUIRED_COPY in t
    for title in ("Pottery Wheel Night", "Axe Throwing Hour", "Escape Room Hour"):
        assert title in t
    assert "$30.00" in t and "<s>$60.00</s>" in t
    assert "Kiln &amp; Co" in t
    assert env.ls.calls   # real scout ran through the fake


def test_required_copy_on_results_and_cart(env):
    pid, host, members = group(env)
    argue(host, pid)
    for path in ("/", f"/p/{pid}/argue"):
        assert REQUIRED_COPY in host.get(path).text, path


def test_argue_third_run_refused(env):
    pid, host, _ = group(env)
    argue(host, pid)
    argue(host, pid)
    assert env.store.argue_count(pid) == 2
    r = argue(host, pid, expect=429)
    assert "two" in r.text.lower()
    assert env.store.argue_count(pid) == 2


def test_scout_runs_on_path_a_and_copies_adult_dropped():
    hits = [p_hit("p1", "Pottery Wheel Night", 3000), p_hit("p2", "Adults Night Out", 2500),
            p_hit("p3", "Escape Room Hour", 2000)]
    env = Env(ls=FakeLs(hits=hits))
    pid, host, _ = group(env)
    argue(host, pid)
    result = env.store.get_result(pid)[1]
    assert result.adult_dropped == 1
    assert all(r.candidate.title != "Adults Night Out" for r in result.shortlist)
    # path A: the fine print came from the agent detail
    assert any(r.candidate.fine_print for r in result.shortlist)


def test_scout_api_error_is_502_and_leaves_state_alone(env):
    env.ls.error = LsApiError(500, message="upstream " + KEY)
    pid, host, _ = group(env)
    r = argue(host, pid, expect=502)
    assert KEY not in r.text and "upstream" not in r.text and "500" not in r.text
    assert env.store.argue_count(pid) == 0 and env.store.get_result(pid) is None
    env.ls.error = None
    argue(host, pid)                    # the failed run did not burn one of the two runs
    assert env.store.argue_count(pid) == 1


def test_missing_key_or_network_failure_is_502(env):
    env.ls.error = RuntimeError("partner key missing")
    pid, host, _ = group(env)
    r = argue(host, pid, expect=502)
    assert "partner key" not in r.text
    env.ls.error = OSError("dns failure")
    argue(host, pid, expect=502)
    assert env.store.argue_count(pid) == 0


def test_unknown_fields_are_marked_unknown():
    hit = p_hit("p1", "Bare Offer", 1500, place="")
    hit["options"][0]["price"].pop("original")
    env = Env(ls=FakeLs(hits=[hit], agent=False))
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Fine print: unknown, check the deal page" in t
    assert "Place: unknown" in t
    assert "Original price: unknown" in t
    assert "Voucher valid until: unknown" in t
    assert "<s>" not in t


def test_api_text_escaped():
    evil = "<script>alert(7)</script>"
    hit = p_hit("p1", "<b>Bold</b> Night " + evil, 3000, place="<i>Hall</i>")
    env = Env(ls=FakeLs(hits=[hit], fine=(evil, "<img src=x>")))
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    for raw in ("<script>alert(7)", "<b>Bold</b>", "<i>Hall</i>", "<img src=x>"):
        assert raw not in t, raw
    assert t.count("&lt;script&gt;alert(7)&lt;/script&gt;") >= 2     # title in the cards and in the debate
    assert "&lt;i&gt;Hall&lt;/i&gt;" in t and "&lt;img src=x&gt;" in t


def test_promo_is_hint_not_price(env):
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Code FALL10 may lower the price" in t
    # the promo is never the price: no price element carries it
    assert not re.search(r'class="price"[^>]*>[^<]*FALL10', t)


def test_voucher_expires_rendered(env):
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Voucher valid until 2026-12-31" in t


def test_fine_print_rendered_from_agent_field(env):
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Appointment required." in t and "Limit 1 per person." in t
    assert "Needs an appointment with the business after purchase" in t


def test_no_ratings_or_reservations_words(env):
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text.lower()
    for word in ("reserve", "reserved", "reservation", "reservations", "rating", "ratings", "review",
                 "reviews", "sold", "bestseller", "best-selling", "popular", "stars"):
        assert not re.search(r"\b%s\b" % re.escape(word), t), word


def test_no_dashes_in_pages(env):
    pid, host, members = group(env)
    argue(host, pid)
    for path in ("/", f"/p/{pid}", f"/p/{pid}/card", f"/p/{pid}/argue", "/static/reveal.js",
                 "/static/style.css"):
        t = host.get(path).text
        assert chr(0x2014) not in t and chr(0x2013) not in t, path


# ---- empty results and relax -------------------------------------------------------------------

def karaoke_env():
    hits = [p_hit("p1", "Karaoke Night", 3000), p_hit("p2", "Karaoke Rooms", 2500)]
    return Env(ls=FakeLs(hits=hits))


def test_empty_result_names_constraint_and_relax_form():
    env = karaoke_env()
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "50", hard_nos="karaoke")
    bo = env.browser()
    card(bo, pid, "Bo", "50")
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "karaoke" in t
    assert 'class="offer"' not in t
    assert f'action="/p/{pid}/relax"' in t
    assert 'value="hard_no:karaoke"' in t
    # only the member who set it gets the button
    assert 'action="/p/%s/relax"' % pid not in bo.get(f"/p/{pid}/argue").text
    # nothing was relaxed behind the group's back
    assert env.store.cards(pid)[0].hard_nos == ["karaoke"]


def test_empty_budget_result_offers_budget_relax(env):
    pid, host, _ = group(env, host_budget="5", others=(("Bo", "5"), ("Cy", "5")))
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert 'value="budget"' in t and 'class="offer"' not in t
    assert "budget" in t.lower()


def test_relax_budget_shows_cheapest_spend(env):
    pid, host, members = group(env, host_budget="5", others=(("Bo", "5"), ("Cy", "5")))
    argue(host, pid)
    r = host.post(f"/p/{pid}/relax", data={"drop": "budget"}, follow_redirects=False)
    assert r.status_code == 200
    assert "$20.00" in r.text and "Escape Room Hour" in r.text   # cheapest option, per person
    assert "own card" in r.text.lower()
    # nothing changed: every card keeps its budget, and no new run happened
    assert [m.max_budget_minor for m in env.store.cards(pid)] == [500, 500, 500]
    assert env.store.argue_count(pid) == 1
    # only the host may ask
    assert members[0].post(f"/p/{pid}/relax", data={"drop": "budget"}).status_code == 403


def test_relax_hard_no_needs_owning_member():
    env = karaoke_env()
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "50")
    bo = env.browser()
    card(bo, pid, "Bo", "50", hard_nos="Karaoke, mimes")
    argue(host, pid)
    form = {"drop": "hard_no:karaoke"}
    # the host did not set it: refused, card untouched
    r = host.post(f"/p/{pid}/relax", data=form)
    assert r.status_code == 403
    # a stranger: refused
    assert env.browser().post(f"/p/{pid}/relax", data=form).status_code == 403
    assert "Karaoke" in env.store.cards(pid)[1].hard_nos
    # the owning member (by cookie) drops only that word
    r = bo.post(f"/p/{pid}/relax", data=form)
    assert r.status_code == 200
    assert env.store.cards(pid)[1].hard_nos == ["mimes"]
    assert env.store.argue_count(pid) == 1          # dropping does not re-run on its own
    argue(host, pid)                                 # the host runs again, run 2 of 2
    assert sorted(top_product_ids(env, pid)) == ["p1", "p2"]


def test_owner_sees_confirm_form_on_card_page():
    env = karaoke_env()
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "50")
    bo = env.browser()
    card(bo, pid, "Bo", "50", hard_nos="karaoke")
    argue(host, pid)
    assert 'value="hard_no:karaoke"' in bo.get(f"/p/{pid}/card").text
    assert "hard_no:karaoke" not in host.get(f"/p/{pid}/card").text


def test_relax_rejects_unknown_drop(env):
    pid, host, _ = group(env)
    argue(host, pid)
    assert host.post(f"/p/{pid}/relax", data={"drop": "everything"}).status_code == 400
    assert host.post(f"/p/{pid}/relax", data={}).status_code == 400


def test_thin_results_suggest_nearby_city():
    env = Env(ls=FakeLs(hits=[p_hit("p1", "Pottery Wheel Night", 3000)]))
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Evanston" in t and "Oak Park" in t
    assert "only 1 offer" in t.lower()
    assert set(NEARBY["Chicago"]) <= {"Evanston", "Oak Park"}


def test_no_candidates_at_all_says_so_and_never_pads():
    env = Env(ls=FakeLs(hits=[]))
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert 'class="offer"' not in t
    assert "Evanston" in t and REQUIRED_COPY in t


# ---- votes and picks ---------------------------------------------------------------------------

def test_vote_requires_member_and_known_option(env):
    pid, host, members = group(env)
    argue(host, pid)
    assert env.browser().post(f"/p/{pid}/vote", data={"product_id": "p1"}).status_code == 403
    assert members[0].post(f"/p/{pid}/vote", data={"product_id": "zzz"}).status_code == 400
    r = members[0].post(f"/p/{pid}/vote", data={"product_id": "p1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{pid}/argue"
    assert env.store.votes(pid) == {"p1": 1}
    members[0].post(f"/p/{pid}/vote", data={"product_id": "p2"})     # a member's vote can change
    assert env.store.votes(pid) == {"p2": 1}


def test_vote_clear_leader_wins(env):
    pid, host, members = group(env)
    argue(host, pid)
    host.post(f"/p/{pid}/vote", data={"product_id": "p2"})
    members[0].post(f"/p/{pid}/vote", data={"product_id": "p2"})
    members[1].post(f"/p/{pid}/vote", data={"product_id": "p1"})
    t = members[1].get(f"/p/{pid}/argue").text
    assert "The group picked: Axe Throwing Hour" in t
    assert "2 votes" in t and "1 vote" in t


def test_vote_tie_host_picks(env):
    pid, host, members = group(env)
    argue(host, pid)
    ranked = top_product_ids(env, pid)
    a, b = ranked[0], ranked[1]
    members[0].post(f"/p/{pid}/vote", data={"product_id": a})
    members[1].post(f"/p/{pid}/vote", data={"product_id": b})
    t = host.get(f"/p/{pid}/argue").text
    assert f'action="/p/{pid}/pick"' in t                       # the host sees the tie-break form
    assert f'action="/p/{pid}/pick"' not in members[0].get(f"/p/{pid}/argue").text
    # no host pick yet and one member still out: the highest-scoring tied option only leads
    assert env.store.get_pick(pid) is None
    first = next(r.candidate.title for r in env.store.get_result(pid)[1].top if r.candidate.product_id == a)
    assert f"Leading so far: {first}" in t and "The group picked" not in t
    # only the host can pick, and only among the tied options
    assert members[0].post(f"/p/{pid}/pick", data={"product_id": b}).status_code == 403
    assert host.post(f"/p/{pid}/pick", data={"product_id": ranked[2]}).status_code == 400
    r = host.post(f"/p/{pid}/pick", data={"product_id": b}, follow_redirects=False)
    assert r.status_code == 303
    assert env.store.get_pick(pid) == b
    second = next(r.candidate.title for r in env.store.get_result(pid)[1].top if r.candidate.product_id == b)
    assert f"The group picked: {second}" in members[1].get(f"/p/{pid}/argue").text


def test_tie_with_all_votes_in_and_no_pick_goes_to_highest_score(env):
    pid, host, members = group(env)
    argue(host, pid)
    ranked = top_product_ids(env, pid)
    host.post(f"/p/{pid}/vote", data={"product_id": ranked[0]})
    members[0].post(f"/p/{pid}/vote", data={"product_id": ranked[1]})
    members[1].post(f"/p/{pid}/vote", data={"product_id": ranked[2]})
    first = env.store.get_result(pid)[1].top[0].candidate.title
    t = host.get(f"/p/{pid}/argue").text
    assert f"The group picked: {first}" in t and "highest score wins" in t


def test_no_votes_no_winner(env):
    pid, host, _ = group(env)
    argue(host, pid)
    assert "The group picked" not in host.get(f"/p/{pid}/argue").text


def test_argue_page_without_result_redirects_to_plan(env):
    c = env.browser()
    pid = new_plan(c)
    r = c.get(f"/p/{pid}/argue", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{pid}"


# ---- expiry -----------------------------------------------------------------------------------

def test_expired_plan_410(env):
    pid, host, members = group(env)
    argue(host, pid)
    env.advance(days=15)
    probes = [("get", f"/p/{pid}"), ("get", f"/p/{pid}/card"), ("post", f"/p/{pid}/card"),
              ("get", f"/p/{pid}/argue"), ("post", f"/p/{pid}/argue"), ("post", f"/p/{pid}/vote"),
              ("post", f"/p/{pid}/pick"), ("post", f"/p/{pid}/relax")]
    for method, path in probes:
        r = getattr(host, method)(path, data={"name": "x", "budget": "5", "product_id": "p1",
                                              "drop": "budget"}, follow_redirects=False) \
            if method == "post" else host.get(path, follow_redirects=False)
        assert r.status_code == 410, (method, path, r.status_code)
        assert "expired" in r.text.lower()
        assert members[0].get(f"/p/{pid}").status_code == 410


def test_missing_plan_410_not_500(env):
    c = env.browser()
    for path in ("/p/doesnotexist0000", "/p/doesnotexist0000/card", "/p/doesnotexist0000/argue",
                 "/p/%3Cscript%3E"):
        r = c.get(path)
        assert r.status_code == 410 and "expired" in r.text.lower(), path
    assert c.post("/p/doesnotexist0000/argue").status_code == 410


def test_store_plan_not_found_on_write_is_410():
    class Racy(SqliteStore):
        def put_card(self, plan_id, member, cap=None):
            raise PlanNotFound(plan_id)

        def set_pick(self, plan_id, product_id):
            raise PlanNotFound(plan_id)

    env = Env(store=Racy(":memory:"))
    c = env.browser()
    pid = new_plan(c)
    r = c.post(f"/p/{pid}/card", data={"name": "Ann", "budget": "50"})
    assert r.status_code == 410 and "expired" in r.text.lower()


def test_purge_runs_at_most_once_per_hour():
    calls = []

    class Counting(SqliteStore):
        def purge_expired(self, now):
            calls.append(now)
            return super().purge_expired(now)

    env = Env()
    env.store = Counting(":memory:", now=lambda: env.clock[0])
    env.app = create_app(store=env.store, ls_client=env.ls, now=lambda: env.clock[0])
    c = env.browser()
    c.get("/")
    c.get("/healthz")
    c.get("/")
    assert len(calls) == 1
    env.advance(minutes=59)
    c.get("/")
    assert len(calls) == 1
    env.advance(minutes=2)
    c.get("/")
    assert len(calls) == 2


# ---- secrets, handlers, skeleton -----------------------------------------------------------------

def test_no_key_anywhere(monkeypatch, caplog):
    """Real LsClient with a fake transport: the key is live in memory and in request headers."""
    monkeypatch.setenv("LS_PARTNER_KEY", KEY)
    seen_headers = []

    def transport(method, url, headers, body, timeout):
        seen_headers.append(dict(headers))
        if "/partner_storefront/products/search" in url:
            import json
            hits = [p_hit("p1", "Pottery Wheel Night", 3000), p_hit("p2", "Axe Throwing Hour", 2500)]
            return 200, {}, json.dumps({"data": hits, "hasMore": False}).encode()
        if "/ls_agent_api/offers/search" in url:
            return 200, {}, b'{"offers": []}'
        return 404, {}, b"{}"

    env = Env(ls=LsClient(transport=transport, sleep=lambda s: None))
    caplog.set_level(logging.DEBUG)
    pid, host, members = group(env)
    argue(host, pid)
    members[0].post(f"/p/{pid}/vote", data={"product_id": "p1"})
    assert any("Authorization" in h for h in seen_headers)      # the key really was in play
    responses = []
    for b in [host] + members:
        for path in ("/", f"/p/{pid}", f"/p/{pid}/card", f"/p/{pid}/argue", "/healthz", "/static/style.css",
                     "/static/reveal.js", "/p/missing0000000000", "/_probe/canary"):
            responses.append(b.get(path))
    for r in responses:
        assert KEY not in r.text
        assert "SECRETVALUE" not in r.text
        assert all(KEY not in str(v) for v in list(r.headers.values()))
    assert KEY not in caplog.text and "SECRETVALUE" not in caplog.text
    assert KEY not in str(env.store.get_plan(pid)) and KEY not in str(env.store.get_result(pid)[1].to_dict())


def test_no_key_in_error_pages_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("LS_PARTNER_KEY", KEY)
    caplog.set_level(logging.DEBUG)
    env = Env(ls=FakeLs(error=LsApiError(401, message="bad token " + KEY)))
    pid, host, _ = group(env)
    r = argue(host, pid, expect=502)
    assert KEY not in r.text and "SECRETVALUE" not in r.text and "SECRETVALUE" not in caplog.text


def test_no_inline_handlers(env):
    pid, host, members = group(env)
    argue(host, pid)
    members[0].post(f"/p/{pid}/vote", data={"product_id": "p1"})
    pages = [host.get(p) for p in ("/", f"/p/{pid}", f"/p/{pid}/card", f"/p/{pid}/argue")]
    pages.append(host.post(f"/p/{pid}/relax", data={"drop": "budget"}))
    pages.append(env.browser().get("/p/gone0000gone0000"))
    pages.append(env.browser().post(f"/p/{pid}/argue"))
    for r in pages:
        assert not INLINE_HANDLER.search(r.text), r.text[:200]
        assert "javascript:" not in r.text.lower()
        assert "<script>" not in r.text
        assert "style=" not in r.text


def test_mobile_viewport_and_scripts(env):
    t = env.browser().get("/").text
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in t
    assert '<script src="/static/reveal.js" defer></script>' in t


def test_healthz_and_probes_still_work(env, monkeypatch):
    c = env.browser()
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/_probe/whoami").status_code == 404
    monkeypatch.setenv("PROBES_ENABLED", "1")
    assert c.get("/_probe/whoami").status_code == 200


def test_oversized_body_rejected(env):
    r = env.browser().post("/plans", data={"city": "Chicago", "date_text": "x" * 100000, "group_size": "3"})
    assert r.status_code == 413


# ---- fix round 1 -----------------------------------------------------------------------------------

def test_argue_double_submit_claims_once(env):
    """A second POST that lands while the first is still scouting must not get the run too."""
    pid, host, _ = group(env)
    argue(host, pid)                              # run 1 of 2 done
    inner = []
    original = env.ls.search_partner

    def slow_scout(q, city, max_price_dollars=None, limit=50):
        if not inner:                             # the double click arrives mid-scout
            inner.append(host.post(f"/p/{pid}/argue", follow_redirects=False))
        return original(q, city, max_price_dollars, limit)

    env.ls.search_partner = slow_scout
    r = argue(host, pid)                          # run 2 claims the last run before it scouts
    assert r.status_code == 303
    assert inner[0].status_code == 429            # the duplicate is refused, not queued
    assert env.store.argue_count(pid) == 2
    assert len([c for c in env.ls.calls if c[0] == "partner"]) >= 1
    argue(host, pid, expect=429)
    assert env.store.argue_count(pid) == 2


def test_argue_failed_scout_releases_run(env):
    pid, host, _ = group(env)
    argue(host, pid)
    env.ls.error = LsApiError(503, message="down")
    argue(host, pid, expect=502)
    assert env.store.argue_count(pid) == 1        # the failed attempt gave its claim back
    env.ls.error = OSError("dns")
    argue(host, pid, expect=502)
    assert env.store.argue_count(pid) == 1
    env.ls.error = None
    argue(host, pid)                              # the run is still available
    assert env.store.argue_count(pid) == 2
    argue(host, pid, expect=429)


def test_rerun_clears_votes_and_pick(env):
    pid, host, members = group(env)
    argue(host, pid)
    ranked = top_product_ids(env, pid)
    members[0].post(f"/p/{pid}/vote", data={"product_id": ranked[0]})
    members[1].post(f"/p/{pid}/vote", data={"product_id": ranked[1]})
    host.post(f"/p/{pid}/pick", data={"product_id": ranked[1]})
    assert env.store.get_pick(pid) == ranked[1]
    argue(host, pid)
    assert env.store.votes(pid) == {} and env.store.get_pick(pid) is None
    t = host.get(f"/p/{pid}/argue").text
    assert "The group picked" not in t and "Leading so far" not in t


def kids_env():
    return Env(ls=FakeLs(fine=("Must be 21+ to enter.",)))


def test_no_kids_wording_anywhere():
    # 1) offers left but one member has kids; 2) every offer is 21+ so the kids rule empties the list
    for env in (Env(), kids_env()):
        host = env.browser()
        pid = new_plan(host, group_size="2")
        card(host, pid, "Ann", "50", kids=True, constraints="two kids, one is 4")
        bo = env.browser()
        card(bo, pid, "Bo", "50")
        argue(host, pid)
        pages = [b.get(p).text for b in (host, bo, env.browser()) for p in (f"/p/{pid}", f"/p/{pid}/argue")]
        for t in pages:
            low = t.lower()
            assert "kid" not in low and "child" not in low, low[low.find("kid") - 80:low.find("kid") + 80]
    t = bo.get(f"/p/{pid}/argue").text
    assert "Some 21+ offers were left out so the plan suits everyone." in t


def test_security_headers(env):
    c = env.browser()
    pid = new_plan(c)
    paths = ["/", f"/p/{pid}", f"/p/{pid}/card", f"/p/{pid}/argue", "/healthz", "/static/style.css",
             "/p/gone0000gone0000"]
    for path in paths:
        r = c.get(path, follow_redirects=False)
        csp = r.headers["content-security-policy"]
        for part in ("default-src 'self'", "img-src 'self' https: data:", "frame-ancestors 'none'",
                     "base-uri 'none'", "form-action 'self'"):
            assert part in csp, (path, csp)
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "same-origin", path
    bad = c.post("/plans", data={}, headers={"sec-fetch-site": "cross-site"})
    assert bad.status_code == 403 and "content-security-policy" in bad.headers


@pytest.mark.parametrize("headers", [{"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                                     {"origin": "https://evil.example"}, {"origin": "null"},
                                     {"sec-fetch-site": "same-origin", "origin": "https://evil.example"}])
def test_cross_site_plan_post_rejected(env, headers):
    c = env.browser()
    form = {"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""}
    r = c.post("/plans", data=form, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert env.store.count_all_plans(T0 - timedelta(days=1)) == 0
    pid = new_plan(c)
    r = env.browser().post(f"/p/{pid}/card", data={"name": "X", "budget": "5"}, headers=headers,
                           follow_redirects=False)
    assert r.status_code == 403 and env.store.cards(pid) == []


@pytest.mark.parametrize("headers", [{}, {"sec-fetch-site": "same-origin"}, {"sec-fetch-site": "none"},
                                     {"origin": "https://testserver"},
                                     {"origin": "https://app.example", "x-forwarded-host": "app.example"}])
def test_same_origin_posts_allowed(env, headers):
    form = {"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""}
    r = env.browser().post("/plans", data=form, headers=headers, follow_redirects=False)
    assert r.status_code == 303


def test_pick_rejects_zero_vote_product_but_allows_when_no_votes(env):
    pid, host, members = group(env)
    argue(host, pid)
    a, b, c3 = top_product_ids(env, pid)
    # no votes at all: the host may pick any of the top options
    assert host.post(f"/p/{pid}/pick", data={"product_id": c3}, follow_redirects=False).status_code == 303
    members[0].post(f"/p/{pid}/vote", data={"product_id": a})
    members[1].post(f"/p/{pid}/vote", data={"product_id": b})
    # votes exist: a product with 0 votes is refused, a tied one is accepted
    assert host.post(f"/p/{pid}/pick", data={"product_id": c3}).status_code == 400
    assert host.post(f"/p/{pid}/pick", data={"product_id": b}, follow_redirects=False).status_code == 303
    assert env.store.get_pick(pid) == b
    # and a clear leader cannot be overridden
    host.post(f"/p/{pid}/vote", data={"product_id": a})
    assert host.post(f"/p/{pid}/pick", data={"product_id": b}).status_code == 400


def test_drop_hard_no_button_only_for_owner():
    env = karaoke_env()
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "50")
    bo = env.browser()
    card(bo, pid, "Bo", "50", hard_nos="karaoke")
    argue(host, pid)
    form = f'action="/p/{pid}/relax"'
    assert form not in host.get(f"/p/{pid}/argue").text
    assert form not in env.browser().get(f"/p/{pid}/argue").text
    owner = bo.get(f"/p/{pid}/argue").text
    assert form in owner and 'value="hard_no:karaoke"' in owner
    # the constraint is named to everyone, but the word is only for the member who set it
    assert "emptied the list" in host.get(f"/p/{pid}/argue").text
    assert "karaoke" not in host.get(f"/p/{pid}/argue").text.lower()


def test_results_visible_without_js_or_when_script_fails(env):
    pid, host, _ = group(env)
    argue(host, pid)
    html = host.get(f"/p/{pid}/argue").text
    results_tag = re.search(r'<section id="results"[^>]*>', html).group(0)
    assert "pending" not in results_tag and "hidden" not in results_tag and "display" not in results_tag
    assert re.search(r"<noscript>.*?class=\"line", html, re.S)            # lines also visible without JS
    assert re.search(r'id="skip-debate"[^>]*\bhidden\b', html)             # a dead button is not shown
    js = host.get("/static/reveal.js").text
    css = host.get("/static/style.css").text
    # the page hides the picks only from script, only when the template API exists, and un-hides on any error
    assert 'results.classList.add("pending")' in js and "catch" in js
    assert js.count('classList.remove("pending")') >= 1 and "tpl.content" in js
    assert ".results.pending" in css and "display: none" in css
    assert not re.search(r"(?m)^\.results\s*\{[^}]*display:\s*none", css)   # never hidden by default


def test_limit_warning_counts_separate_checkouts():
    hit = p_hit("p1", "Pottery Wheel Night", 3000)
    env = Env(ls=FakeLs(hits=[hit], fine=("Limit 2 per person.",)))
    pid, host, _ = group(env)                        # a group of 3 needs 3 vouchers, limit 2 each
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Your group needs 3, so this takes 2 separate checkouts." in t
    env = Env(ls=FakeLs(hits=[hit], fine=("Limit 1 per person.",)))
    pid, host, _ = group(env)
    argue(host, pid)
    assert "Your group needs 3, so this takes 3 separate checkouts." in host.get(f"/p/{pid}/argue").text


def test_card_cap_is_enforced_by_the_store():
    class LastSeat(SqliteStore):
        """Plan looked open to the route, but the store says the last seat just went."""

        def put_card(self, plan_id, member, cap=None):
            assert cap == 2
            return False

    env = Env(store=LastSeat(":memory:"))
    c = env.browser()
    pid = new_plan(c, group_size="2")
    r = c.post(f"/p/{pid}/card", data={"name": "Ann", "budget": "50"}, follow_redirects=False)
    assert r.status_code == 409 and "full" in r.text.lower()
    assert "m_" + pid not in c.cookies


# ---- final review wave -------------------------------------------------------------------------------

CSP = "Content-Security-Policy"


def test_vercel_without_kv_returns_503(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(web, "default_store", lambda: pytest.fail("fell back to a per-instance store"))
    c = TestClient(create_app(), base_url="https://testserver")
    form = {"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""}
    for r in (c.get("/"), c.get("/p/abc"), c.get("/p/abc/argue"), c.get("/admin/carts.csv"),
              c.post("/plans", data=form), c.post("/demo", data={"city": "Chicago"})):
        assert r.status_code == 503, r.request.url
        assert "GroupThink is not fully set up yet (storage missing)." in r.text
        assert CSP in r.headers
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json() == {"ok": False, "storage": "missing"}
    # with Upstash configured the same deployment is healthy
    monkeypatch.setattr(web, "default_store", lambda: SqliteStore(":memory:"))
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example.test")
    monkeypatch.setenv("KV_REST_API_TOKEN", "tok")
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_local_without_kv_still_uses_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    assert isinstance(web.default_store(), SqliteStore)
    c = TestClient(create_app(), base_url="https://testserver")
    assert c.get("/").status_code == 200
    assert c.get("/healthz").json() == {"ok": True}


def test_kv_error_renders_503_with_security_headers():
    class Down(SqliteStore):
        def get_plan(self, plan_id):
            raise KvError("KV HTTP 500: upstash-secret-detail")

    c = Env(store=Down(":memory:")).browser()
    for r in (c.get("/p/abc"), c.post("/p/abc/card", data={"name": "Ann", "budget": "5"})):
        assert r.status_code == 503
        assert "upstash-secret-detail" not in r.text and "KvError" not in r.text
        assert "try again" in r.text.lower()
        assert "default-src 'self'" in r.headers[CSP]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["cache-control"] == "no-store"


def test_trusted_ip_header_env_override(monkeypatch):
    def req(headers):
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                        "client": ("8.8.8.8", 1)})
    both = {"x-real-ip": "1.1.1.1", "x-vercel-forwarded-for": "5.5.5.5", "x-forwarded-for": "2.2.2.2"}
    monkeypatch.delenv("TRUSTED_IP_HEADER", raising=False)
    assert client_ip(req(both)) == "1.1.1.1"                       # default stays x-real-ip
    monkeypatch.setenv("TRUSTED_IP_HEADER", "X-Vercel-Forwarded-For")
    assert client_ip(req(both)) == "5.5.5.5"
    assert client_ip(req({"x-real-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2"})) == "2.2.2.2"
    monkeypatch.setenv("TRUSTED_IP_HEADER", "  ")
    assert client_ip(req(both)) == "1.1.1.1"


def test_emptying_hard_no_word_only_visible_to_owner():
    env = karaoke_env()
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "50")
    bo = env.browser()
    card(bo, pid, "Bo", "50", hard_nos="karaoke")
    argue(host, pid)
    for who in (host, env.browser()):
        t = who.get(f"/p/{pid}/argue").text
        assert "a hard no on someone" in t.lower() and "emptied the list" in t
        assert "karaoke" not in t.lower() and "Drop this hard no" not in t
        assert f'action="/p/{pid}/relax"' not in t
    owner = bo.get(f"/p/{pid}/argue").text
    assert "emptied the list" in owner
    assert "karaoke" in owner and "Drop this hard no" in owner and 'value="hard_no:karaoke"' in owner


def test_card_help_text_and_edit_copy(env):
    c = env.browser()
    pid = new_plan(c)
    t = c.get(f"/p/{pid}/card").text
    assert ("A note for yourself. GroupThink does not filter on this yet; use hard no's for things "
            "you must avoid.") in t
    r = c.post(f"/p/{pid}/card", data={"name": "Ann", "budget": "50", "constraints": "vegan"},
               follow_redirects=True)
    assert "You can edit your card until the host reruns the argument." in r.text
    assert "until the host starts" not in r.text
    assert "vegan" in c.get(f"/p/{pid}/card").text             # still the owner's private field
    assert "vegan" not in env.browser().get(f"/p/{pid}").text


def test_bad_content_length_400(env):
    c = env.browser()
    for bad in ("abc", "-", "1.5", "12 34"):
        r = c.post("/plans", content=b"city=Chicago", headers={
            "content-length": bad, "content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 400, bad
        assert "Traceback" not in r.text


# ---- solo (1 player) plans ----------------------------------------------------------------

SOLO_FORM = {"city": "Chicago", "date_text": "Saturday", "theme": ""}


def new_solo(c, ip="203.0.113.10", **over):
    form = dict(SOLO_FORM)
    form.update(over)
    r = c.post("/solo", data=form, headers={"x-real-ip": ip}, follow_redirects=False)
    assert r.status_code == 303, r.text
    m = re.fullmatch(r"/p/([A-Za-z0-9_-]+)/card", r.headers["location"])
    assert m, r.headers["location"]
    return m.group(1)


def solo_ready(env, budget="50", **card_kw):
    """A solo host with a saved card and one finished run. Returns (pid, browser)."""
    c = env.browser()
    pid = new_solo(c)
    card(c, pid, "Ann", budget, **card_kw)
    argue(c, pid)
    return pid, c


def visible_text(html_text):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return t.replace("GroupThink", "").replace("Groupon", "").replace("groupon", "").lower()


SOLO_BANNED = re.compile(r"group|everyone|vote|\bhost\b")


def no_group_words(html_text):
    hit = SOLO_BANNED.search(visible_text(html_text))
    assert not hit, hit.group(0)


def test_solo_creates_one_seat_plan_and_goes_to_card(env):
    c = env.browser()
    r = c.post("/solo", data=SOLO_FORM, follow_redirects=False)
    assert r.status_code == 303
    m = re.fullmatch(r"/p/([A-Za-z0-9_-]+)/card", r.headers["location"])
    assert m
    pid = m.group(1)
    plan = env.store.get_plan(pid)
    assert plan["group_size"] == 1 and plan["mode"] == "solo" and plan["demo"] is False
    sc = r.headers["set-cookie"].lower()
    assert f"host_{pid}=".lower() in sc and "httponly" in sc and "secure" in sc
    assert web.plan_mode(plan) == "solo"


def test_group_and_demo_plans_store_group_mode(env):
    c = env.browser()
    pid = new_plan(c)
    assert env.store.get_plan(pid)["mode"] == "group"
    r = c.post("/demo", data={"city": "Chicago"}, follow_redirects=False)
    assert env.store.get_plan(plan_id_of(r))["mode"] == "group"
    assert web.plan_mode({"city": "x"}) == "group"


def test_solo_rate_limit(env):
    c = env.browser()
    for _ in range(20):
        new_solo(c, ip="198.51.100.1")
    r = c.post("/solo", data=SOLO_FORM, headers={"x-real-ip": "198.51.100.1"}, follow_redirects=False)
    assert r.status_code == 429
    new_solo(c, ip="198.51.100.2")


def test_solo_rejects_cross_site_post(env):
    r = env.browser().post("/solo", data=SOLO_FORM, headers={"sec-fetch-site": "cross-site"},
                           follow_redirects=False)
    assert r.status_code == 403
    assert env.store.count_all_plans(T0 - timedelta(days=1)) == 0


@pytest.mark.parametrize("over", [{"city": "Atlantis"}, {"city": "other", "other_city": "  "},
                                  {"date_text": ""}])
def test_solo_rejects_bad_city(env, over):
    form = dict(SOLO_FORM)
    form.update(over)
    r = env.browser().post("/solo", data=form, follow_redirects=False)
    assert r.status_code == 400
    assert REQUIRED_COPY in r.text
    assert env.store.count_all_plans(T0 - timedelta(days=1)) == 0


def test_solo_other_city_uses_free_text(env):
    pid = new_solo(env.browser(), city="other", other_city="Denver")
    assert env.store.get_plan(pid)["city"] == "Denver"


def test_solo_second_browser_cannot_add_card(env):
    c = env.browser()
    pid = new_solo(c)
    card(c, pid, "Ann")
    r = card(env.browser(), pid, "Bo", expect=409)
    assert "full" in r.text.lower()
    assert len(env.store.cards(pid)) == 1


def test_solo_plan_page_goes_to_card_or_fight(env):
    c = env.browser()
    pid = new_solo(c)
    r = c.get(f"/p/{pid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{pid}/card"
    card(c, pid, "Ann")
    r = c.get(f"/p/{pid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{pid}/card"
    argue(c, pid)
    r = c.get(f"/p/{pid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{pid}/argue"
    for path in (f"/p/{pid}/card", f"/p/{pid}/argue"):
        t = c.get(path).text
        assert "Copy link" not in t and "share-url" not in t


def test_solo_card_save_offers_fight(env):
    c = env.browser()
    pid = new_solo(c)
    r = card(c, pid, "Ann")
    t = c.get(r.headers["location"]).text
    assert f'action="/p/{pid}/argue"' in t and "Start the fight" in t
    assert "Copy link" not in t and "share-url" not in t
    assert not re.search(r"cards? in|of 1 cards", t.lower())
    no_group_words(t)
    argue(c, pid)
    assert c.get(f"/p/{pid}/card").status_code == 200


def test_solo_card_page_before_saving_has_no_group_words(env):
    c = env.browser()
    pid = new_solo(c)
    t = c.get(f"/p/{pid}/card").text
    assert "Start the fight" not in t
    no_group_words(t)


def test_solo_argue_page(env):
    pid, c = solo_ready(env)
    result = env.store.get_result(pid)[1]
    t = c.get(f"/p/{pid}/argue").text
    assert "Your Saver" in t
    assert "WINNER" in t and 'class="winner"' in t
    assert result.top[0].candidate.title in re.search(r'<p class="winner".*?</p>', t, re.S).group(0)
    assert t.count("Pick this one instead") == len(result.top) - 1
    assert "/vote" not in t
    no_group_words(t)
    assert REQUIRED_COPY in t
    assert f'action="/p/{pid}/cart"' in t          # the pick is final, the cart is one click away


def test_solo_argue_page_stranger_cannot_pick_or_cart(env):
    pid, c = solo_ready(env)
    t = env.browser().get(f"/p/{pid}/argue").text
    assert "Pick this one instead" not in t and "Get the cart link" not in t
    assert "WINNER" in t


def test_solo_budget_emptied_page_is_solo_worded(env):
    pid, c = solo_ready(env, budget="5")
    t = c.get(f"/p/{pid}/argue").text
    assert 'value="budget"' in t and 'class="offer"' not in t
    assert "No offer fits your budget" in t
    no_group_words(t)
    r = c.post(f"/p/{pid}/relax", data={"drop": "budget"})
    assert r.status_code == 200 and "$20.00" in r.text
    no_group_words(r.text)


def test_solo_hard_no_relax_still_works():
    env = karaoke_env()
    c = env.browser()
    pid = new_solo(c)
    card(c, pid, "Ann", "50", hard_nos="karaoke")
    argue(c, pid)
    t = c.get(f"/p/{pid}/argue").text
    assert 'value="hard_no:karaoke"' in t and 'class="offer"' not in t
    no_group_words(t)
    r = c.post(f"/p/{pid}/relax", data={"drop": "hard_no:karaoke"})
    assert r.status_code == 200
    no_group_words(r.text)
    assert env.store.cards(pid)[0].hard_nos == []
    argue(c, pid)
    assert sorted(top_product_ids(env, pid)) == ["p1", "p2"]


def test_solo_pages_have_no_dashes(env):
    pid, c = solo_ready(env)
    for path in ("/", f"/p/{pid}/card", f"/p/{pid}/argue"):
        t = c.get(path).text
        assert chr(0x2014) not in t and chr(0x2013) not in t, path


def test_group_form_still_requires_two(env):
    r = env.browser().post("/plans", data={"city": "Chicago", "date_text": "Sat", "group_size": "1",
                                           "theme": ""}, follow_redirects=False)
    assert r.status_code == 400


def test_solo_tally_shape():
    class C:
        def __init__(self, pid):
            self.product_id = pid

    class R:
        def __init__(self, pid):
            self.candidate = C(pid)

    class Res:
        top = [R("a"), R("b"), R("c")]

    t = web.solo_tally(Res, None, frozenset())
    assert t["winner"] == "a" and t["reason"] == "the referee's pick" and t["final"] is True
    assert t["counts"] == {"a": 0, "b": 0, "c": 0}
    assert set(t) == set(web.tally(Res.top, {}, None))
    t = web.solo_tally(Res, "b", frozenset())
    assert t["winner"] == "b" and t["reason"] == "your pick"
    assert web.solo_tally(Res, "zzz", frozenset())["winner"] == "a"
    assert web.solo_tally(Res, "a", frozenset({"a"}))["winner"] == "b"
    assert web.solo_tally(Res, None, frozenset({"a", "b", "c"}))["winner"] is None


def test_home_has_one_player_form_with_unique_ids(env):
    t = env.browser().get("/").text
    assert 'action="/solo"' in t and "1 PLAYER" in t
    ids = re.findall(r'\sid="([^"]+)"', t)
    assert len(ids) == len(set(ids)), ids
    for prefix in ("solo-", "group-", "demo-"):
        assert any(i.startswith(prefix + "city") for i in ids), prefix
