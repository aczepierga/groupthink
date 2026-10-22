from datetime import date

from groupthink.candidates import Candidate
from groupthink.facts import Facts
from groupthink.fit import (Excluded, Member, Ranked, RankResult, Verdict, fixed_date, parse_plan_date,
                            per_member_spend_minor, quantity_needed, rank)


def cand(pid="p1", oid="o1", title="Thing", price=8000, original=None, party=2, bound="exactly",
         min_units=1, max_units=None, facts=None, highlights=None, categories=None, fine_print=None,
         option_title=""):
    return Candidate(
        product_id=pid, option_id=oid, permalink=None, title=title, option_title=option_title, merchant=None,
        place="", city="", price_minor=price, original_minor=original, precision=2,
        party_size=party, party_bound=bound, min_units=min_units, max_units=max_units,
        fine_print=fine_print, highlights=highlights or [], categories=categories or [],
        voucher_expires=None, promo_code=None, facts=facts or Facts())


def member(mid="m1", cap=100000, likes=None, hard_nos=None, kids=False, name="Ann"):
    return Member(id=mid, name=name, max_budget_minor=cap, likes=likes or [],
                  hard_nos=hard_nos or [], kids=kids, constraints="", share_numbers=True)


def group(n, **kw):
    return [member(mid=f"m{i}", **kw) for i in range(n)]


# --- quantity -----------------------------------------------------------------------------

def test_quantity_exactly_2_group_5():
    assert quantity_needed(5, 2, "exactly", 1) == 3


def test_quantity_unknown_party():
    assert quantity_needed(4, None, None, 1) == 4


def test_quantity_up_to_is_group_size():
    assert quantity_needed(4, 4, "up_to", 1) == 4
    res = rank([cand(party=4, bound="up_to")], group(4))
    assert "may_cover_more" in res.shortlist[0].warnings
    assert res.shortlist[0].quantity == 4


def test_quantity_min_units():
    assert quantity_needed(1, 1, "exactly", 2) == 2


def test_quantity_group_of_one():
    assert quantity_needed(1, 2, "exactly", 1) == 1
    assert quantity_needed(0, 2, "exactly", 1) == 1


def test_option_covers_more_than_group():
    c = cand(price=9000, party=6, bound="exactly")
    assert quantity_needed(3, 6, "exactly", 1) == 1
    assert per_member_spend_minor(c, 1, 3) == 3000


# --- budget -------------------------------------------------------------------------------

def test_budget_per_member_spend():
    c = cand(price=8000, party=2, bound="exactly")
    fit = rank([c], group(2, cap=4000)).shortlist[0]
    assert all(v.fits_budget for v in fit.verdicts)
    over = rank([c], group(2, cap=3999)).shortlist[0]
    assert [v.over_by_minor for v in over.verdicts] == [1, 1]
    assert not any(v.fits_budget for v in over.verdicts)


def test_over_budget_stays_in_shortlist_not_top():
    res = rank([cand(price=8000)], group(2, cap=100))
    assert len(res.shortlist) == 1
    assert res.top == []
    assert res.emptied_by == "budget"


def test_emptied_by_budget():
    res = rank([cand(pid="a", price=9000), cand(pid="b", price=9500)], group(2, cap=10))
    assert res.top == [] and len(res.shortlist) == 2 and res.emptied_by == "budget"


def test_fitting_options_rank_in_top_and_emptied_none():
    res = rank([cand(pid="a", price=100), cand(pid="b", price=900000)], group(2, cap=5000))
    assert [r.candidate.product_id for r in res.top] == ["a"]
    assert res.emptied_by is None


def test_discount_and_tie_by_lower_spend():
    # equal score (same 50% discount, both fit); cheaper has the HIGHER id so only spend orders it first
    cheap = cand(pid="z", price=1000, original=2000)
    pricey = cand(pid="a", price=2000, original=4000)
    res = rank([pricey, cheap], group(2, cap=100000))
    assert res.shortlist[0].score == res.shortlist[1].score
    assert res.shortlist[0].candidate.product_id == "z"
    assert res.shortlist[0].discount_pct == 50


def test_spend_rounds_up_non_divisible():
    c = cand(price=8001, party=2, bound="exactly")   # group 2 -> qty 1, 8001/2 = 4000.5
    assert per_member_spend_minor(c, 1, 2) == 4001
    v = rank([c], group(2, cap=4000)).shortlist[0].verdicts[0]
    assert v.over_by_minor == 1 and v.fits_budget is False


def test_likes_raise_score():
    a = cand(pid="a", title="Bowling Night")
    b = cand(pid="b", title="Pottery Class")
    res = rank([a, b], group(2, likes=["pottery"]))
    assert res.shortlist[0].candidate.product_id == "b"
    assert res.shortlist[0].verdicts[0].likes_matched == ["pottery"]


# --- gates --------------------------------------------------------------------------------

def test_max_units_gate():
    c = cand(party=2, bound="exactly", max_units=2)
    res = rank([c], group(5))          # qty 3 > max 2
    assert res.shortlist == [] and res.excluded[0].reason == "quantity"
    assert res.emptied_by == "quantity"


def test_max_units_none_passes():
    assert len(rank([cand(max_units=None)], group(5)).shortlist) == 1


def test_limit_per_person_is_warning_not_filter():
    c = cand(facts=Facts(limit_per_person=1))
    res = rank([c], group(2))
    assert len(res.shortlist) == 1
    assert "limit_per_person" in res.shortlist[0].warnings


def test_hard_no_word_match():
    spa = cand(pid="a", title="Day Spa Package")
    pasta = cand(pid="b", title="Spaghetti Night")
    res = rank([spa, pasta], [member(hard_nos=["spa"])])
    assert [r.candidate.product_id for r in res.shortlist] == ["b"]
    assert res.excluded == [Excluded("a", "Day Spa Package", "hard_no:spa")]


def test_hard_no_matches_categories_and_highlights_case_insensitive():
    c1 = cand(pid="a", categories=["Skydiving"])
    c2 = cand(pid="b", highlights=["Includes SKYDIVING gear"])
    res = rank([c1, c2], [member(hard_nos=["skydiving"])])
    assert res.shortlist == [] and res.emptied_by == "hard_no:skydiving"


def test_emptied_by_hard_no():
    res = rank([cand(title="Spa Day")], [member(hard_nos=["spa"])])
    assert res.emptied_by == "hard_no:spa" and res.top == []


def test_kids_excludes_21_only_when_true():
    ms = [member(kids=True)]
    yes = rank([cand(facts=Facts(age_21=True))], ms)
    assert yes.shortlist == [] and yes.emptied_by == "kids_21"
    assert yes.excluded[0].reason == "kids_21"
    unk = rank([cand(facts=Facts(age_21=None))], ms)
    assert len(unk.shortlist) == 1 and "age_unknown" in unk.shortlist[0].warnings
    no = rank([cand(facts=Facts(age_21=False))], ms)
    assert len(no.shortlist) == 1 and "age_unknown" not in no.shortlist[0].warnings
    nokids = rank([cand(facts=Facts(age_21=True))], [member(kids=False)])
    assert len(nokids.shortlist) == 1


def test_appointment_warnings_tristate():
    ms = [member()]
    t = rank([cand(facts=Facts(appointment=True))], ms).shortlist[0].warnings
    u = rank([cand(facts=Facts(appointment=None))], ms).shortlist[0].warnings
    f = rank([cand(facts=Facts(appointment=False))], ms).shortlist[0].warnings
    assert "appointment" in t and "appointment_unknown" not in t
    assert "appointment_unknown" in u and "appointment" not in u
    assert "appointment" not in f and "appointment_unknown" not in f


def test_one_option_per_product():
    a1 = cand(pid="p", oid="o1", price=1000)
    a2 = cand(pid="p", oid="o2", price=500)
    b = cand(pid="q", oid="o3", price=700)
    res = rank([a1, a2, b], group(2))
    pids = [r.candidate.product_id for r in res.shortlist]
    assert sorted(pids) == ["p", "q"]
    p = next(r for r in res.shortlist if r.candidate.product_id == "p")
    assert p.candidate.option_id == "o2"


def test_shortlist_cap_5_top_cap_3():
    cands = [cand(pid=f"p{i}", price=100 + i) for i in range(8)]
    res = rank(cands, group(2, cap=100000))
    assert len(res.shortlist) == 5 and len(res.top) == 3
    assert res.adult_dropped == 0


def test_rank_empty_inputs():
    assert rank([], group(2)).emptied_by is None
    assert rank([cand()], []).shortlist  # no members: no crash


# --- hostile text -------------------------------------------------------------------------

def test_hostile_text_truncated_and_inert():
    m = Member.clean(id="x", name="N" * 500, max_budget_minor=5000,
                     likes=["<script>alert(1)</script>", "L" * 2000, "\U0001F355" * 1000],
                     hard_nos=["H" * 2000, "", "   "], kids=False, constraints="C" * 2000,
                     share_numbers=True)
    assert len(m.name) == 40
    assert all(len(s) <= 280 for s in m.likes + m.hard_nos)
    assert len(m.constraints) == 280
    assert "" not in m.hard_nos
    res = rank([cand(title="Pizza \U0001F355 Night")], [m])      # must not raise
    assert len(res.shortlist) == 1


def test_clean_never_raises_on_empty_or_odd():
    m = Member.clean(id="x", name=None, max_budget_minor="abc", likes=None, hard_nos=[],
                     kids=None, constraints=None, share_numbers=None)
    assert m.name == "" and m.max_budget_minor == 0 and m.likes == [] and m.kids is False
    rank([cand()], [m])


def test_regex_metachars_in_hard_no_are_inert():
    m = member(hard_nos=["(", "[a-", "a.*"])
    assert len(rank([cand(title="Plain")], [m]).shortlist) == 1


# --- round trips --------------------------------------------------------------------------

def test_roundtrips():
    m = member(likes=["pizza"])
    assert Member.from_dict(m.to_dict()) == m
    res = rank([cand(facts=Facts(appointment=True))], [m])
    r = res.shortlist[0]
    assert Ranked.from_dict(r.to_dict()) == r
    assert RankResult.from_dict(res.to_dict()) == res
    v = Verdict("m", True, 0, [])
    assert Verdict.from_dict(v.to_dict()) == v
    e = Excluded("p", "t", "quantity")
    assert Excluded.from_dict(e.to_dict()) == e


def test_fit_everyone_option_below_shortlist_cut_is_found():
    """Six offers that one member loves outscore the one that fits both budgets, so the 5-item cut drops it."""
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    loud = [cand(pid="L%d" % i, title=" ".join(words), price=5000, party=1) for i in range(6)]
    quiet = cand(pid="Q", title="Plain Tuesday", price=500, party=1)
    members = [member("m1", cap=10000, likes=words), member("m2", cap=1000)]
    res = rank(loud + [quiet], members)
    assert res.emptied_by is None
    assert [r.candidate.product_id for r in res.top] == ["Q"]
    assert all(all(v.fits_budget for v in r.verdicts) for r in res.top)
    ids = [r.candidate.product_id for r in res.shortlist]
    assert "Q" in ids and 5 <= len(ids) <= 5 + 3        # appended, never more than TOP_N over the cut
    # no survivor fits everyone: still "budget"
    res = rank(loud, members)
    assert res.top == [] and res.emptied_by == "budget" and len(res.shortlist) == 5


def test_top_is_one_option_per_product_and_at_most_three():
    opts = [cand(pid="A", oid="a1", title="A deal", price=500, party=1),
            cand(pid="A", oid="a2", title="A deal", price=600, party=1)]
    others = [cand(pid=p, title=p + " deal", price=700 + i, party=1) for i, p in enumerate("BCDEFG")]
    res = rank(opts + others, group(2, cap=5000))
    tops = [r.candidate.product_id for r in res.top]
    assert len(tops) == 3 and len(set(tops)) == 3
    shortlist_ids = [id(r) for r in res.shortlist]
    assert all(id(r) in shortlist_ids for r in res.top)


# --- voucher size from the title, fixed dates ---------------------------------------------

TODAY = date(2026, 10, 22)


def test_quantity_uses_title_party_when_agent_size_missing():
    assert quantity_needed(3, None, None, 1, title_party=8) == 1
    assert quantity_needed(5, None, None, 1, title_party=4) == 2
    assert quantity_needed(5, 2, "exactly", 1, title_party=8) == 3     # agent data wins
    assert quantity_needed(3, None, None, 2, title_party=8) == 2       # min_units still applies


def test_fixed_date_reads_one_event_date():
    assert fixed_date("Oct 23rd Reggaeton Cruise", TODAY) == "2026-10-23"
    assert fixed_date("Halloween Cruise November 1st", TODAY) == "2026-11-01"
    assert fixed_date("Oct 19th Comedy Night", TODAY) == "2026-10-19"
    assert fixed_date("Feb 2 Gala", TODAY) == "2027-02-02"


def test_fixed_date_ignores_ranges_and_lowercase():
    for text in ("General Admission (Valid Oct 8 - 31)", "Valid Nov 1 to Dec 31", "Open May 1st through Sept",
                 "Groups of 8 may enjoy", "Pickleball Class", "Mar 2026 Pass", "Nov 7, 8 Show"):
        assert fixed_date(text, TODAY) is None, text


def test_parse_plan_date():
    assert parse_plan_date("Sat Nov 14", TODAY) == "2026-11-14"
    assert parse_plan_date("Sat Nov 7, evening", TODAY) == "2026-11-07"
    for text in ("11/14", "this weekend", "Nov 14-16", "Nov 7 or 8"):
        assert parse_plan_date(text, TODAY) is None, text


def test_rank_excludes_wrong_and_past_dates():
    cands = [cand(pid="a", title="Oct 23rd Cruise"), cand(pid="b", title="November 1st Cruise"),
             cand(pid="c", title="Oct 19th Comedy"), cand(pid="d", title="Pottery")]
    res = rank(cands, group(2), today=TODAY, plan_date_text="Sat Nov 14")
    assert {e.product_id: e.reason for e in res.excluded} == {"a": "date", "b": "date", "c": "date"}
    assert [r.candidate.product_id for r in res.shortlist] == ["d"]
    res = rank(cands, group(2), today=TODAY, plan_date_text="this weekend")
    assert {e.product_id: e.reason for e in res.excluded} == {"c": "date"}
    kept = {r.candidate.product_id: r for r in res.shortlist}
    assert "fixed_date:2026-10-23" in kept["a"].warnings
    assert "fixed_date:2026-11-01" in kept["b"].warnings
    assert not any(w.startswith("fixed_date") for w in kept["d"].warnings)
    same = rank(cands[:1], group(2), today=TODAY, plan_date_text="Fri Oct 23")
    assert same.shortlist and not any(w.startswith("fixed_date") for w in same.shortlist[0].warnings)


def test_title_party_sets_quantity_and_drops_cover_more_warning():
    c = cand(title="Swing Suite for 8", party=None, bound=None)
    c.title_party = 8
    r = rank([c], group(3)).shortlist[0]
    assert r.quantity == 1 and "may_cover_more" not in r.warnings


def test_fixed_date_comma_time_is_not_a_second_day():
    assert fixed_date("Sat Oct 24, 7pm", TODAY) == "2026-10-24"
    assert fixed_date("Nov 7, 8pm", TODAY) == "2026-11-07"
    assert fixed_date("Nov 7, 8 PM", TODAY) == "2026-11-07"
    assert fixed_date("Nov 7, 8", TODAY) is None
    assert fixed_date("Nov 7/8 show", TODAY) is None
    c = cand(title="Show", pid="x")
    c.option_title = "Oct 23, 7pm Show"
    from groupthink.fit import offer_fixed_date
    assert offer_fixed_date(c, TODAY) == "2026-10-23"


# --- word matching, every blocking word, younger-guest offers (Task 3) -------------------------

def test_hard_no_matches_plurals_and_option_title():
    r = rank([cand(title="Arcade Games Night")], [member(hard_nos=["game"])])
    assert not r.shortlist and r.excluded[0].reason == "hard_no:game"
    r = rank([cand(title="Fun Evening", option_title="Pickleball Classes")], [member(hard_nos=["class"])])
    assert not r.shortlist and r.excluded[0].reason == "hard_no:class"
    r = rank([cand(title="Party Night")], [member(hard_nos=["art"])])
    assert r.shortlist and not r.excluded


def test_likes_match_plurals():
    r = rank([cand(title="Pizzas Galore")], [member(likes=["pizza"])])
    assert r.shortlist[0].verdicts[0].likes_matched == ["pizza"]
    assert r.likes_matched_any is True
    r = rank([cand(title="Bowling")], [member(likes=["pizza"])])
    assert r.likes_matched_any is False


def test_blocking_words_lists_every_word_per_member():
    ann = member("a", hard_nos=["spa", "massage", "spa"], name="Ann")
    bo = member("b", hard_nos=["tour"], name="Bo")
    two_options = [cand(pid="p1", oid="o1", title="Spa Massage Tour"),
                   cand(pid="p1", oid="o2", title="Spa Massage Tour")]
    r = rank(two_options, [ann, bo])
    assert r.emptied_by == "hard_no:spa"
    got = sorted((b["member_id"], b["word"]) for b in r.blocking_words)
    assert got == [("a", "massage"), ("a", "spa"), ("b", "tour")]
    assert RankResult.from_dict(r.to_dict()).blocking_words == r.blocking_words


def test_old_result_without_new_fields_loads():
    d = rank([cand()], group(1)).to_dict()
    for k in ("blocking_words", "young_dropped", "likes_matched_any"):
        d.pop(k)
    r = RankResult.from_dict(d)
    assert (r.blocking_words, r.young_dropped, r.likes_matched_any) == ([], 0, True)


def test_young_only_excluded_for_adult_groups():
    c = cand(title="Halloween Spooky Kids Cruise")
    r = rank([c], group(2))
    assert not r.shortlist and r.excluded[0].reason == "young_only"
    assert r.emptied_by == "young_only" and r.young_dropped == 1
    assert rank([c], [member("m0", kids=True), member("m1")]).shortlist
    r = rank([cand(title="Evening Cruise", option_title="Children Under 12")], group(2))
    assert r.excluded and r.excluded[0].reason == "young_only"


def test_young_only_keeps_adult_offers():
    kept = [cand(pid="a", title="Bowling Night", categories=["Youth Sports"]),
            cand(pid="b", title="Kids Eat Free Brunch"), cand(pid="c", title="Kid-friendly Brunch"),
            cand(pid="d", title="Junior's Cheesecake"), cand(pid="e", title="Fountain of Youth Spa"),
            cand(pid="f", title="Junior Suite")]
    r = rank(kept, group(2))
    assert not r.excluded and r.young_dropped == 0
    assert len(r.shortlist) == 5 and len(r.top) == 3
