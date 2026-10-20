from groupthink.candidates import Candidate
from groupthink.facts import Facts
from groupthink.fit import (Excluded, Member, Ranked, RankResult, Verdict, per_member_spend_minor,
                            quantity_needed, rank)


def cand(pid="p1", oid="o1", title="Thing", price=8000, original=None, party=2, bound="exactly",
         min_units=1, max_units=None, facts=None, highlights=None, categories=None, fine_print=None):
    return Candidate(
        product_id=pid, option_id=oid, permalink=None, title=title, option_title="", merchant=None,
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
    cheap = cand(pid="a", price=1000, original=2000)
    pricey = cand(pid="b", price=2000, original=4000)
    res = rank([pricey, cheap], group(2, cap=100000))
    assert res.shortlist[0].candidate.product_id == "a"
    assert res.shortlist[0].discount_pct == 50


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
