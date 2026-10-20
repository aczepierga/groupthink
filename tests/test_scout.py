import pytest

from groupthink.ls_client import LsApiError
from groupthink.scout import (ADULT_TERMS, DETAIL_CAP, NEARBY, confirm_match, is_adult, match_agent,
                              scout)


def hit(pid, title="Swedish Massage", city="Chicago", cats=("Day Spas",), prices=(4080,), **kw):
    h = {"id": pid, "title": title, "status": "active", "availabilityRequired": False,
         "categoryLabels": list(cats),
         "locations": [{"id": "l1", "name": "Place", "city": city, "state": "Illinois"}],
         "options": [{"id": "%s-o%d" % (pid, i), "title": "Opt %d" % i, "active": True, "minUnits": 1,
                      "maxUnits": None,
                      "price": {"retail": p, "original": p * 2, "currency": "USD", "currencyPrecision": 2}}
                     for i, p in enumerate(prices)]}
    h.update(kw)
    return h


def summary(sid, title="Swedish Massage", city="Chicago"):
    return {"id": sid, "permalink": "pl-" + sid, "title": title, "city": city}


def detail(sid, prices=(4080,), **kw):
    d = {"id": sid, "permalink": "pl-" + sid, "merchant": "Royal Day Spa",
         "finePrint": ["Appointment required."], "highlights": [],
         "options": [{"id": "ao%d" % i, "title": "A", "price": {"amountMinor": p, "currency": "USD",
                                                               "currencyExponent": 2},
                      "partySize": None, "voucherExpires": None, "promoCode": None}
                     for i, p in enumerate(prices)]}
    d.update(kw)
    return d


class FakeClient:
    def __init__(self, partner=None, agent=None, offers=None, offer_error=None, agent_error=None):
        self.partner = partner or {}      # q -> hits
        self.agent = agent or {}          # q -> summaries
        self.offers = offers or {}        # permalink -> detail
        self.offer_error, self.agent_error = offer_error, agent_error
        self.partner_calls, self.agent_calls, self.offer_calls = [], [], []

    def search_partner(self, q, city, max_price_dollars=None, limit=50):
        self.partner_calls.append((q, city, max_price_dollars))
        return list(self.partner.get(q, []))

    def search_agent(self, q, city, limit=50):
        self.agent_calls.append((q, city))
        if self.agent_error:
            raise self.agent_error
        return list(self.agent.get(q, []))

    def get_agent_offer(self, permalink, city=None):
        self.offer_calls.append(permalink)
        if self.offer_error:
            raise self.offer_error
        return self.offers[permalink]


def run(client, theme="massage", path="B", cap=5000, size=2, city="Chicago"):
    return scout(client, city, theme, cap, size, path)


# ---- queries ---------------------------------------------------------

def test_zero_then_eight_uses_second_query():
    eight = [hit("p%d" % i, title="Thing %d" % i) for i in range(8)]
    c = FakeClient(partner={"massage": [], "things to do": eight})
    r = run(c)
    assert r.queries_tried[:2] == ["massage", "things to do"]
    assert len(r.candidates) == 8
    assert {x.product_id for x in r.candidates} == {"p%d" % i for i in range(8)}


def test_empty_theme_dropped_and_queries_deduped():
    c = FakeClient()
    assert run(c, theme="  ").queries_tried == ["things to do", "dinner", "activities"]
    c = FakeClient()
    assert run(c, theme="Dinner").queries_tried == ["Dinner", "things to do", "activities"]


def test_stops_at_30_unique_products():
    c = FakeClient(partner={"massage": [hit("p%d" % i, title="T%d" % i) for i in range(30)]})
    r = run(c)
    assert r.queries_tried == ["massage"]
    assert len(c.partner_calls) == 1


def test_stops_after_four_queries():
    c = FakeClient()
    r = run(c)
    assert r.queries_tried == ["massage", "things to do", "dinner", "activities"]


def test_dedupe_by_product_id():
    same = hit("p1")
    c = FakeClient(partner={"massage": [same, same], "things to do": [hit("p1"), hit("p2", title="Other")]})
    r = run(c)
    assert sorted({x.product_id for x in r.candidates}) == ["p1", "p2"]
    assert len([x for x in r.candidates if x.product_id == "p1"]) == 1


def test_max_price_dollars():
    c = FakeClient()
    run(c, cap=1999, size=3)
    assert c.partner_calls[0][2] == 60            # ceil(1999 * 3 / 100) = ceil(59.97)
    c = FakeClient()
    run(c, cap=1500, size=2)
    assert c.partner_calls[0][2] == 30            # exact, no rounding up
    c = FakeClient()
    run(c, cap=1501, size=2)
    assert c.partner_calls[0][2] == 31            # 30.02 rounds up


def test_partner_search_uses_city():
    c = FakeClient()
    run(c, city="Austin")
    assert {call[1] for call in c.partner_calls} == {"Austin"}


# ---- thin ------------------------------------------------------------

def test_thin_flag_under_5():
    four = [hit("p%d" % i, title="T%d" % i) for i in range(4)]
    assert run(FakeClient(partner={"massage": four})).thin is True
    five = four + [hit("p4", title="T4")]
    assert run(FakeClient(partner={"massage": five})).thin is False


def test_nearby_towns():
    assert NEARBY == {"Chicago": ["Evanston", "Oak Park"], "Austin": ["Round Rock"],
                      "New York": ["Brooklyn", "Jersey City"]}


# ---- adult filter ----------------------------------------------------

THC = hit("thc", title="Up to 50% Off on Infused Pastries at Prohibition THC Cafe", cats=["Bakeries"])
SEX = hit("sex", title="Explore Human Sexuality: General Admission to Unique Educational Sex Museum "
                       "The Sexploratorium",
          cats=["Family", "Museums & Galleries", "Museums & Attractions", "Museums"])
NIGHT = hit("night", title="Adults Night: 1-Hour Jump & Beer at Trampoline Park in West Town or Roscoe "
                           "Village (Up to 32% Off)", cats=["Youth Sports", "Trampoline & Bounce Houses"])


def test_adult_known_misses_dropped():
    ok = hit("ok", title="Bowling")
    r = run(FakeClient(partner={"massage": [THC, SEX, NIGHT, ok]}))
    assert [c.product_id for c in r.candidates] == ["ok"]
    assert r.adult_dropped == 3


def test_adult_match_on_category_only():
    h = hit("cat", title="Great Evening", cats=["Cannabis Tours"])
    assert is_adult(h) is True
    r = run(FakeClient(partner={"massage": [h]}))
    assert r.candidates == [] and r.adult_dropped == 1


def test_strip_steak_not_dropped():
    steak = hit("steak", title="NY Strip Steak Dinner", cats=["Steakhouses"])
    r = run(FakeClient(partner={"massage": [steak]}))
    assert [c.product_id for c in r.candidates] == ["steak"]
    assert r.adult_dropped == 0


def test_adult_terms_are_whole_words():
    assert is_adult(hit("x", title="Essex Sussex Tour", cats=["Tours"])) is False
    assert is_adult(hit("y", title="Hookah Lounge Night", cats=["Lounges"])) is True
    assert "21+ night" in ADULT_TERMS
    assert is_adult(hit("z", title="21+ Night at the Club", cats=["Nightlife"])) is True


# ---- path A': matching ----------------------------------------------

def test_title_match_normalized():
    h = hit("p1", title="Swedish or Deep-Tissue Massage!")
    s = summary("s1", title="  swedish OR deeptissue   massage ")
    assert match_agent(h, [summary("s0", title="Something Else"), s]) is s


def test_city_must_match():
    h = hit("p1", city="Chicago")
    assert match_agent(h, [summary("s1", city="Evanston")]) is None
    assert match_agent(h, [summary("s1", city=None)]) is None
    assert match_agent(h, [summary("s1", city="chicago")]) is not None
    assert match_agent(hit("p2", locations=[]), [summary("s1")]) is None


def test_ambiguous_title_and_city_match_is_none():
    assert match_agent(hit("p1"), [summary("s1"), summary("s2")]) is None


def test_price_set_must_match():
    h = hit("p1", prices=(4080, 5805))
    assert confirm_match(h, detail("s1", prices=(4080, 5805, 9999))) is True   # extra agent option ok
    assert confirm_match(h, detail("s1", prices=(4080,))) is False             # 5805 missing
    assert confirm_match(h, detail("s1", prices=(4080, 5806))) is False
    # inactive Partner options are not required to match
    h["options"][1]["active"] = False
    assert confirm_match(h, detail("s1", prices=(4080,))) is True
    assert confirm_match(h, {"options": None}) is False
    assert confirm_match(hit("p9", prices=()), detail("s1")) is False          # nothing to confirm


def test_no_match_stays_partner_only():
    h = hit("p1")
    other = summary("s9", title="Different Deal")
    c = FakeClient(partner={"massage": [h]}, agent={"massage": [other]})
    r = run(c, path="A")
    assert c.offer_calls == []
    [cand] = r.candidates
    assert cand.fine_print is None and cand.permalink is None and cand.merchant is None


def test_matched_and_confirmed_attaches_agent_facts():
    h = hit("p1", prices=(4080, 5805))
    c = FakeClient(partner={"massage": [h]}, agent={"massage": [summary("s1")]},
                   offers={"pl-s1": detail("s1", prices=(4080, 5805))})
    r = run(c, path="A")
    assert c.offer_calls == ["pl-s1"]
    assert {x.option_id for x in r.candidates} == {"p1-o0", "p1-o1"}
    assert all(x.fine_print == ["Appointment required."] and x.permalink == "pl-s1" and
               x.merchant == "Royal Day Spa" for x in r.candidates)


def test_price_mismatch_in_flow_stays_partner_only():
    c = FakeClient(partner={"massage": [hit("p1", prices=(4080, 5805))]},
                   agent={"massage": [summary("s1")]},
                   offers={"pl-s1": detail("s1", prices=(4080, 7000))})
    r = run(c, path="A")
    assert c.offer_calls == ["pl-s1"]
    assert all(x.fine_print is None for x in r.candidates)


def test_detail_without_permalink_gets_summary_permalink():
    d = detail("s1")
    del d["permalink"]
    c = FakeClient(partner={"massage": [hit("p1")]}, agent={"massage": [summary("s1")]},
                   offers={"pl-s1": d})
    [cand] = run(c, path="A").candidates
    assert cand.permalink == "pl-s1"


def test_detail_cap_12():
    hits = [hit("p%02d" % i, title="Deal %02d" % i) for i in range(15)]
    sums = [summary("s%02d" % i, title="Deal %02d" % i) for i in range(15)]
    offers = {"pl-s%02d" % i: detail("s%02d" % i) for i in range(15)}
    c = FakeClient(partner={"massage": hits}, agent={"massage": sums}, offers=offers)
    r = run(c, path="A")
    assert DETAIL_CAP == 12
    assert c.offer_calls == ["pl-s%02d" % i for i in range(12)]          # highest Partner rank first
    withfp = {x.product_id for x in r.candidates if x.fine_print is not None}
    assert withfp == {"p%02d" % i for i in range(12)}
    assert all(x.fine_print is None for x in r.candidates if x.product_id in ("p12", "p13", "p14"))


def test_cap_counts_matched_products_not_unmatched_ones():
    hits = [hit("n%d" % i, title="Nomatch %d" % i) for i in range(5)] + \
           [hit("m%02d" % i, title="Deal %02d" % i) for i in range(14)]
    sums = [summary("s%02d" % i, title="Deal %02d" % i) for i in range(14)]
    offers = {"pl-s%02d" % i: detail("s%02d" % i) for i in range(14)}
    c = FakeClient(partner={"massage": hits}, agent={"massage": sums}, offers=offers)
    run(c, path="A")
    assert len(c.offer_calls) == 12


def test_agent_search_once_per_query_with_same_q_and_city():
    c = FakeClient(partner={"massage": [hit("p1")]})
    run(c, path="A", city="Chicago")
    assert c.agent_calls == [(q, "Chicago") for q in ["massage", "things to do", "dinner", "activities"]]


def test_summaries_from_later_query_are_used():
    c = FakeClient(partner={"massage": [hit("p1")]}, agent={"dinner": [summary("s1")]},
                   offers={"pl-s1": detail("s1")})
    [cand] = run(c, path="A").candidates
    assert cand.fine_print == ["Appointment required."]


def test_path_b_never_calls_agent_api():
    c = FakeClient(partner={"massage": [hit("p1")]}, agent={"massage": [summary("s1")]},
                   offers={"pl-s1": detail("s1")})
    r = run(c, path="B")
    assert c.agent_calls == [] and c.offer_calls == []
    assert r.candidates[0].fine_print is None


def test_agent_failures_degrade_to_partner_only():
    c = FakeClient(partner={"massage": [hit("p1")]}, agent_error=LsApiError(500, "internal"))
    r = run(c, path="A")
    assert len(r.candidates) == 1 and r.candidates[0].fine_print is None
    c = FakeClient(partner={"massage": [hit("p1")]}, agent={"massage": [summary("s1")]},
                   offer_error=LsApiError(404, "notFound"))
    r = run(c, path="A")
    assert len(r.candidates) == 1 and r.candidates[0].fine_print is None


def test_adult_and_unsellable_hits_do_not_spend_detail_fetches():
    sold_out = hit("so", title="Sold Out Deal", availabilityRequired=True)
    c = FakeClient(partner={"massage": [THC, sold_out]},
                   agent={"massage": [summary("s1", title="Sold Out Deal")]},
                   offers={"pl-s1": detail("s1")})
    run(c, path="A")
    assert c.offer_calls == []


def test_unknown_path_rejected():
    with pytest.raises(ValueError):
        run(FakeClient(), path="C")
