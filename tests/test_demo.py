"""Demo path (sample friends), the safe deal link, and the age-limit fact line. Same fakes as test_web."""
import re

import pytest

from groupthink.candidates import Candidate
from groupthink.facts import Facts
from test_web import (T0, Env, FakeLs, _env, a_detail, argue, env, new_plan, p_hit, plan_id_of)  # noqa: F401

DEAL = "https://customers.livingsocial.com/deals/pottery-wheel-night-abc123"
BUY = "https://customers.livingsocial.com/buy/026f627e-3f35-4d0c-a7ba-6b0c0e7cd7c1"
BANNER = ("Demo plan: Ana and Ben are sample people so you can see the argument alone. "
          "The offers are real and live.")


def demo(c, ip="203.0.113.10", city="Chicago", headers=None, **form):
    data = {"city": city}
    data.update(form)
    h = {"x-real-ip": ip}
    h.update(headers or {})
    return c.post("/demo", data=data, headers=h, follow_redirects=False)


def demo_plan(c, **kw):
    r = demo(c, **kw)
    assert r.status_code == 303, r.text
    return plan_id_of(r)


class LinkLs(FakeLs):
    """Agent detail carries an offerUrl (and a buyUrl that must never be linked)."""

    def __init__(self, offer_url, **kw):
        super().__init__(**kw)
        self.offer_url = offer_url

    def get_agent_offer(self, permalink, city=None):
        d = super().get_agent_offer(permalink, city)
        d["buyUrl"] = BUY
        if self.offer_url is not None:
            d["offerUrl"] = self.offer_url
        return d


def one_offer_page(ls):
    env = Env(ls=ls)
    c = env.browser()
    pid = demo_plan(c)
    argue(c, pid)
    return c.get(f"/p/{pid}/argue").text


# ---- the demo plan ---------------------------------------------------------------------------

def test_demo_creates_plan_with_two_labelled_samples(env):
    c = env.browser()
    r = demo(c, city="Austin")
    pid = plan_id_of(r)
    plan = env.store.get_plan(pid)
    assert (plan["city"], plan["date_text"], plan["group_size"], plan["theme"]) == (
        "Austin", "This weekend", 3, "fun night out")
    sc = r.headers["set-cookie"].lower()
    assert f"host_{pid}=".lower() in sc and f"m_{pid}=".lower() in sc
    assert sc.count("httponly") == 2 and sc.count("secure") >= 2 and sc.count("samesite=lax") == 2
    cards = {m.name: m for m in env.store.cards(pid)}
    assert set(cards) == {"You", "Sample: Ana", "Sample: Ben"}
    assert (cards["Sample: Ana"].max_budget_minor, cards["Sample: Ana"].likes, cards["Sample: Ana"].hard_nos,
            cards["Sample: Ana"].share_numbers) == (4500, ["food", "drinks"], ["heights"], True)
    ben = cards["Sample: Ben"]
    assert (ben.max_budget_minor, ben.likes, ben.hard_nos, ben.kids, ben.share_numbers) == (
        8000, ["games", "active"], [], False, False)
    you = cards["You"]
    assert (you.max_budget_minor, you.likes, you.hard_nos, you.share_numbers) == (6000, [], [], True)
    # the visitor is the host AND the member "You": can open the card and start the argument
    assert c.cookies.get(f"m_{pid}") == you.id
    assert "Edit my card" in c.get(f"/p/{pid}").text
    assert c.get(f"/p/{pid}/card").status_code == 200
    argue(c, pid)


def test_demo_banner_says_offers_are_real(env):
    c = env.browser()
    pid = demo_plan(c)
    t = c.get(f"/p/{pid}").text
    assert BANNER in t
    assert "Let them argue" in t
    assert "Sample: Ana" in t and "Sample: Ben" in t              # names keep the prefix everywhere
    assert BANNER not in c.get(f"/p/{new_plan(env.browser())}").text   # a normal plan has no banner


def test_demo_respects_plan_rate_limit(env):
    c = env.browser()
    for _ in range(20):
        demo_plan(c, ip="198.51.100.1")
    assert demo(c, ip="198.51.100.1").status_code == 429
    assert "198.51.100.1" not in demo(c, ip="198.51.100.1").text
    demo_plan(c, ip="198.51.100.2")                      # another client is unaffected


def test_demo_and_plans_share_one_limit(env):
    c = env.browser()
    for _ in range(10):
        new_plan(c, ip="198.51.100.3")
        demo_plan(c, ip="198.51.100.3")
    assert demo(c, ip="198.51.100.3").status_code == 429
    r = c.post("/plans", data={"city": "Chicago", "date_text": "Sat", "group_size": "3", "theme": ""},
               headers={"x-real-ip": "198.51.100.3"}, follow_redirects=False)
    assert r.status_code == 429


def test_demo_counts_against_daily_cap(env, monkeypatch):
    from groupthink import limits
    monkeypatch.setattr(limits, "PLANS_PER_DAY_ALL", 2)
    c = env.browser()
    demo_plan(c, ip="198.51.100.20")
    demo_plan(c, ip="198.51.100.21")
    assert demo(c, ip="198.51.100.22").status_code == 429


@pytest.mark.parametrize("headers", [{"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                                     {"origin": "https://evil.example"}, {"origin": "null"}])
def test_demo_rejects_cross_site_post(env, headers):
    c = env.browser()
    r = demo(c, headers=headers)
    assert r.status_code == 403
    assert env.store.count_all_plans(T0.replace(hour=0)) == 0
    assert not c.cookies


@pytest.mark.parametrize("city", ["Atlantis", "", "other", "chicago", "Chicago<script>"])
def test_demo_rejects_unknown_city(env, city):
    r = demo(env.browser(), city=city)
    assert r.status_code == 400
    assert env.store.count_all_plans(T0.replace(hour=0)) == 0


def test_demo_missing_city_is_400(env):
    r = env.browser().post("/demo", data={}, follow_redirects=False)
    assert r.status_code == 400


def test_home_has_secondary_demo_form(env):
    t = env.browser().get("/").text
    assert 'action="/demo"' in t and "Try it with sample friends" in t
    form = re.search(r'<form[^>]*action="/demo".*?</form>', t, re.S).group(0)
    for city in ("Chicago", "Austin", "New York"):
        assert f'value="{city}"' in form
    assert 'value="Chicago" selected' in form
    assert 'value="other"' not in form


def test_sample_prefix_in_debate_lines(env):
    c = env.browser()
    pid = demo_plan(c)
    argue(c, pid)
    lines = env.store.get_result(pid)[0]
    speakers = {ln.speaker for ln in lines}
    advocates = [ln for ln in lines if ln.kind == "advocate"]
    from_samples = [ln for ln in advocates if "Ana" in ln.speaker or "Ben" in ln.speaker]
    assert from_samples, speakers
    assert all(ln.speaker.startswith("Sample: ") for ln in from_samples)
    assert speakers <= {"Scout", "Referee", "You", "Sample: Ana", "Sample: Ben"}
    page = c.get(f"/p/{pid}/argue").text
    assert "Sample: Ana" in page or "Sample: Ben" in page
    assert not re.search(r'class="who">(Ana|Ben)<', page)


# ---- deal link ---------------------------------------------------------------------------------

def test_deal_link_only_for_matched_https_offer_url():
    t = one_offer_page(LinkLs(DEAL))
    link = f'<a href="{DEAL}" target="_blank" rel="noopener noreferrer">See the deal</a>'
    assert t.count(link) == 3                        # one per result card
    assert BUY not in t and "/buy/" not in t and "buyUrl" not in t
    # Partner-only offers (no agent match) keep the fine-print text and get no link
    t = one_offer_page(LinkLs(DEAL, agent=False))
    assert "See the deal" not in t and DEAL not in t
    assert "Fine print: unknown, check the deal page" in t
    # matched, but the agent document has no offerUrl
    assert "See the deal" not in one_offer_page(LinkLs(None))


@pytest.mark.parametrize("url", [
    "http://customers.livingsocial.com/deals/x",            # not https
    "https://evil.example/deals/x",                          # other host
    "https://livingsocial.com.evil.example/deals/x",         # suffix trick
    "https://evillivingsocial.com/deals/x",                  # not a subdomain
    "https://customers.livingsocial.com@evil.example/x",     # userinfo trick
    "https://evil.example\\.livingsocial.com/x",             # backslash: browsers read it as a slash
    "https://customers.livingsocial.com:8443/deals/x",       # odd port
    "javascript:alert(1)",
    "//customers.livingsocial.com/deals/x",
    "/deals/x",
    BUY,                                                     # a buy link is never an offer link
    "https://www.groupon.com/checkout/abc",
    "https://customers.livingsocial.com/deals/x y",
    "https://customers.livingsocial.com/deals/x\n",
    "",
])
def test_deal_link_refuses_unsafe_urls(url):
    t = one_offer_page(LinkLs(url))
    assert "See the deal" not in t
    assert not url or f'href="{url}"' not in t


@pytest.mark.parametrize("url", ["https://www.groupon.com/deals/x", "https://livingsocial.com/deals/x",
                                 "https://customers.livingsocial.com/deals/x?ref=1"])
def test_deal_link_allows_livingsocial_and_groupon_hosts(url):
    assert f'href="{url}"' in one_offer_page(LinkLs(url))


def test_offer_url_roundtrips_and_old_records_load():
    c = Candidate("p", "o", "pl", "T", "O", None, "", "", 100, None, 2, None, None, 1, None, None, [], [],
                  None, None, Facts(), "https://customers.livingsocial.com/deals/x")
    assert Candidate.from_dict(c.to_dict()).offer_url == c.offer_url
    old = c.to_dict()
    del old["offer_url"]                              # a result stored before this field existed
    assert Candidate.from_dict(old).offer_url is None


def test_offer_url_is_filled_from_agent_detail_only_when_matched():
    from groupthink.candidates import build_candidates
    hit = p_hit("p1", "Pottery Wheel Night", 3000)
    agent = dict(a_detail("p1", 3000), offerUrl=DEAL)
    assert build_candidates([hit], {"p1": agent}, "A")[0].offer_url == DEAL
    assert build_candidates([hit], {}, "A")[0].offer_url is None
    assert build_candidates([hit], {"p1": agent}, "B")[0].offer_url is None
    assert build_candidates([hit], {"p1": dict(agent, offerUrl=7)}, "A")[0].offer_url is None


# ---- age limit is a fact, not a warning -------------------------------------------------------

def offer_block(t):
    return re.search(r'<article class="offer".*?</article>', t, re.S).group(0)


def test_age_line_is_fact_not_warning():
    t = one_offer_page(FakeLs(fine=None))            # fine print unknown: age_21 is None
    block = offer_block(t)
    assert "Age limit: unknown" not in t
    assert "Age limit: not stated" in block
    assert block.index("Age limit: not stated") < block.index("<h4>Fine print</h4>")   # in the facts list
    assert "Heads up" in block                       # warnings still render (appointment unknown) ...
    assert "Age limit" not in block.split("<h4>Heads up</h4>")[1]   # ... without the age line in them
    assert "kid" not in t.lower()


def test_age_line_true_false_and_partner_only():
    t = one_offer_page(FakeLs(fine=("Guests must be 21+ to enter.",)))
    assert "21+ only" in offer_block(t) and "Age limit: not stated" not in t
    t = one_offer_page(FakeLs(fine=("Limit 1 per person.",)))                    # age_21 False: no line
    assert "Age limit" not in t and "21+ only" not in t
    t = one_offer_page(FakeLs(agent=False))                                       # Partner-only: unknown
    assert "Age limit: not stated" in offer_block(t) and "Age limit: unknown" not in t


def test_demo_pages_have_no_dashes_or_kids_wording(env):
    c = env.browser()
    pid = demo_plan(c)
    argue(c, pid)
    pages = [env.browser().get("/").text, c.get(f"/p/{pid}").text, c.get(f"/p/{pid}/argue").text]
    for t in pages:
        assert "\u2014" not in t and "\u2013" not in t
        assert "kid" not in t.lower() and "child" not in t.lower()
        assert not re.search(r"\son[a-z]+\s*=", t, re.I) and "<style" not in t and "style=" not in t
