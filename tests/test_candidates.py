import copy
import json
from pathlib import Path

from groupthink.candidates import Candidate, build_candidates, title_party
from groupthink.facts import Facts

FIX = Path(__file__).parent / "fixtures"
HITS = json.loads((FIX / "partner_hits.json").read_text())
MASSAGE, CAFE, ESCAPE, YOGA, SOLD_OUT = [h["id"] for h in HITS]


def agent_offer(options, **kw):
    base = {"id": "agent-1", "permalink": "some-permalink", "merchant": "Royal Day Spa",
            "finePrint": ["Limit 1 per person.", "Appointment required."],
            "highlights": ["Relax"], "options": options}
    base.update(kw)
    return base


def aopt(oid, minor, **kw):
    d = {"id": oid, "title": oid, "price": {"amountMinor": minor, "currency": "USD", "currencyExponent": 2},
         "partySize": None, "voucherExpires": None, "promoCode": None}
    d.update(kw)
    return d


def by_option(cands):
    return {c.option_id: c for c in cands}


def test_availability_required_dropped():
    cands = build_candidates(HITS, {}, "B")
    assert ESCAPE not in {c.product_id for c in cands}


def test_inactive_options_dropped():
    got = by_option(build_candidates(HITS, {}, "B"))
    assert "opt-a3" not in got            # active == False
    assert "opt-b1" in got                # active == None means sellable


def test_not_active_product_and_priceless_option_dropped():
    assert SOLD_OUT not in {c.product_id for c in build_candidates(HITS, {}, "B")}
    h = copy.deepcopy(HITS[0])
    h["options"][0]["price"] = None
    assert "opt-a1" not in by_option(build_candidates([h], {}, "B"))


def test_categories_from_category_labels():
    got = by_option(build_candidates(HITS, {}, "B"))
    assert got["opt-a1"].categories == ["Day Spas", "Swedish Massage", "Deep Tissue Massage"]


def test_price_is_minor_int():
    c = by_option(build_candidates(HITS, {}, "B"))["opt-a1"]
    assert c.price_minor == 4080 and isinstance(c.price_minor, int)
    assert c.original_minor == 8000 and c.precision == 2
    h = copy.deepcopy(HITS[0])
    h["options"][0]["price"]["retail"] = 4080.0
    assert isinstance(by_option(build_candidates([h], {}, "B"))["opt-a1"].price_minor, int)


def test_units_and_place_from_partner():
    got = by_option(build_candidates(HITS, {}, "B"))
    assert (got["opt-a1"].min_units, got["opt-a1"].max_units) == (1, 2)
    assert got["opt-a2"].max_units is None       # null maxUnits = no cap
    assert got["opt-b2"].min_units == 2
    assert (got["opt-a1"].place, got["opt-a1"].city) == ("Royal Day Spa", "Chicago")
    assert got["opt-b1"].place == "22 Oak Ave"   # no name: falls back to street
    assert got["opt-d1"].place == "" and got["opt-d1"].city == ""   # no locations


def test_path_b_fields_none():
    # agent_details is ignored on path B even when it is populated
    details = {MASSAGE: agent_offer([aopt("x", 4080)])}
    for c in build_candidates(HITS, details, "B"):
        assert c.permalink is None and c.merchant is None
        assert c.fine_print is None and c.highlights == []
        assert c.party_size is None and c.party_bound is None
        assert c.voucher_expires is None and c.promo_code is None
        assert c.facts == Facts()                 # tri-state: unknown, not False


def test_path_a_without_agent_detail_keeps_partner_fields():
    got = by_option(build_candidates(HITS, {}, "A"))
    assert got["opt-a1"].price_minor == 4080
    assert got["opt-a1"].fine_print is None and got["opt-a1"].facts.appointment is None


def test_option_attached_by_price():
    details = {MASSAGE: agent_offer([
        aopt("agent-id-unrelated-1", 5805, partySize={"people": 2, "bound": "exactly"},
             voucherExpires="2027-01-01T00:00:00.000Z", promoCode="FALL"),
        aopt("agent-id-unrelated-2", 4080, partySize={"people": 4, "bound": "up_to"}),
    ])}
    got = by_option(build_candidates(HITS, details, "A"))
    a1, a2 = got["opt-a1"], got["opt-a2"]
    assert (a1.party_size, a1.party_bound, a1.promo_code) == (4, "up_to", None)
    assert (a2.party_size, a2.party_bound) == (2, "exactly")
    assert a2.voucher_expires == "2027-01-01T00:00:00.000Z" and a2.promo_code == "FALL"
    # offer-level fields attach to every option of the matched product
    assert a1.permalink == "some-permalink" and a1.merchant == "Royal Day Spa"
    assert a1.highlights == ["Relax"]
    assert a1.facts.limit_per_person == 1 and a1.facts.appointment is True
    # other products untouched
    assert got["opt-b1"].fine_print is None


def test_unmatched_price_keeps_partner_fields():
    details = {MASSAGE: agent_offer([aopt("a", 999)])}
    a1 = by_option(build_candidates(HITS, details, "A"))["opt-a1"]
    assert a1.price_minor == 4080 and (a1.min_units, a1.max_units) == (1, 2)
    assert a1.party_size is None and a1.party_bound is None
    assert a1.voucher_expires is None and a1.promo_code is None
    assert a1.fine_print == ["Limit 1 per person.", "Appointment required."]   # offer-level survives


def test_ambiguous_price_leaves_none():
    details = {MASSAGE: agent_offer([
        aopt("a", 4080, partySize={"people": 1, "bound": "exactly"}, promoCode="X"),
        aopt("b", 4080, partySize={"people": 2, "bound": "exactly"}, promoCode="Y"),
    ])}
    a1 = by_option(build_candidates(HITS, details, "A"))["opt-a1"]
    assert a1.party_size is None and a1.party_bound is None and a1.promo_code is None


def test_at_least_bound_and_null_party_size():
    details = {MASSAGE: agent_offer([
        aopt("a", 4080, partySize={"people": 6, "bound": "at_least"}),
        aopt("b", 5805, partySize=None),
    ])}
    got = by_option(build_candidates(HITS, details, "A"))
    assert (got["opt-a1"].party_size, got["opt-a1"].party_bound) == (6, "at_least")
    assert got["opt-a2"].party_size is None and got["opt-a2"].party_bound is None


def test_empty_fine_print_list_is_false_not_unknown():
    details = {MASSAGE: agent_offer([aopt("a", 4080)], finePrint=[])}
    f = by_option(build_candidates(HITS, details, "A"))["opt-a1"].facts
    assert f.appointment is False
    details = {MASSAGE: agent_offer([aopt("a", 4080)], finePrint=None)}
    assert by_option(build_candidates(HITS, details, "A"))["opt-a1"].facts.appointment is None


def test_roundtrip_to_dict_from_dict():
    details = {MASSAGE: agent_offer([aopt("a", 4080, partySize={"people": 2, "bound": "exactly"})])}
    for c in build_candidates(HITS, details, "A"):
        d = json.loads(json.dumps(c.to_dict()))
        assert Candidate.from_dict(d) == c


def test_hostile_text_passes_through_unchanged():
    # escaping is a display concern (later tasks); here text must not crash or be altered
    h = copy.deepcopy(HITS[0])
    h["title"] = "<script>alert(1)</script> " + "x" * 2000
    c = build_candidates([h], {}, "B")[0]
    assert c.title.startswith("<script>") and len(c.title) > 2000


def test_place_and_city_follow_the_plan_city():
    h = copy.deepcopy(HITS[0])
    h["locations"] = [{"name": "Far", "city": "Denver"}, {"name": "Near", "city": "Chicago, IL"}]
    c = build_candidates([h], {}, "B", plan_city="Chicago")[0]
    assert (c.place, c.city) == ("Near", "Chicago, IL")
    c = build_candidates([h], {}, "B")[0]           # no plan city: the first location, as before
    assert (c.place, c.city) == ("Far", "Denver")


# --- voucher size from the title ----------------------------------------------------------

def test_title_party_reads_people_counts():
    assert title_party("", "Swing Suite for 8") == 8
    assert title_party("Up to 4 Players Per Team", "Scavenger Hunt For Four Teams") == 4
    assert title_party("", "Dinner for Two") == 2
    assert title_party("", "$25 for 2 People") == 2


def test_title_party_ignores_non_people_counts():
    for text in ("2 for $30", "Up to 82% Off", "$20 for $40", "Monthly Unlimited Yoga for 3 Months",
                 "Facial Package for 2 Hours", "Class Pack for 10 Classes", "Pass for 6 Weeks",
                 "Wine Flight for 2 or 4", "Party for 40 People"):
        assert title_party("", text) is None, text
    assert title_party("Distillery Tour for 4 plus $10 token", "Distillery Tour for 2, 4, or 6") is None
