import re
from datetime import date

from groupthink import lines as L
from groupthink.candidates import Candidate
from groupthink.debate import Line, run_debate, run_debate_rounds, run_debate_traced
from groupthink.facts import Facts
from groupthink.fit import Excluded, Member, rank
from groupthink.money import fmt

TODAY = date(2026, 10, 20)
BANNED = ["reservation", "reserve", "booked", "rating", "review", "popular", "best-selling",
          "top-rated", "bestseller", "sold", "just over", "so close", "a bit", "call", "—", "–"]


def cand(pid="p1", oid="o1", title="Clay Wheel Night", price=3000, original=None, party=1,
         bound="exactly", min_units=1, max_units=None, facts=None, expires=None,
         highlights=None, categories=None):
    return Candidate(
        product_id=pid, option_id=oid, permalink=None, title=title, option_title="", merchant=None,
        place="", city="Chicago", price_minor=price, original_minor=original, precision=2,
        party_size=party, party_bound=bound, min_units=min_units, max_units=max_units,
        fine_print=None, highlights=highlights or [], categories=categories or [],
        voucher_expires=expires, promo_code=None, facts=facts or Facts())


def member(mid, name, cap=100000, likes=None, hard_nos=None, kids=False, share=True):
    return Member(id=mid, name=name, max_budget_minor=cap, likes=likes or [],
                  hard_nos=hard_nos or [], kids=kids, constraints="", share_numbers=share)


def lines_of(lines, speaker):
    return [ln for ln in lines if ln.speaker == speaker]


def texts(lines):
    return [ln.text for ln in lines]


# --- fixtures -----------------------------------------------------------------------------

def chicago():
    members = [member("m0", "Ann", cap=6000, likes=["pottery"]),
               member("m1", "Bo", cap=4000, share=False),
               member("m2", "Cy", cap=9000)]
    cands = [
        cand("p1", title="Pottery Wheel Night at Kiln & Co", price=4500, party=1, bound="exactly"),
        cand("p2", title="Axe Throwing for Three", price=9000, original=15000, party=3,
             bound="exactly", facts=Facts(appointment=True), expires="2026-11-15"),
        cand("p3", title="Sunset Boat Cruise", price=5200, party=None, bound=None),
        cand("p4", title="Escape Room Hour", price=2400, party=1, bound="exactly",
             facts=Facts(limit_per_person=1)),
    ]
    return members, rank(cands, members)


def scenario(code):
    """One-candidate group where `code` is the strongest fact for member m0."""
    m = [member("m0", "Ann", cap=100000, likes=[]), member("m1", "Bo", cap=100000)]
    kw = dict(price=2000, party=1, bound="exactly")
    if code == "over_budget":
        m = [member("m0", "Ann", cap=500), member("m1", "Bo", cap=100000)]
    elif code == "limit_per_person":
        kw["facts"] = Facts(limit_per_person=1)
    elif code == "appointment":
        kw["facts"] = Facts(appointment=True)
    elif code == "may_cover_more":
        kw.update(party=2, bound="up_to")
    elif code == "matches_like":
        m = [member("m0", "Ann", likes=["clay"]), member("m1", "Bo")]
    elif code == "best_discount":
        kw["original"] = 4000
    elif code == "voucher_soon":
        kw["expires"] = "2026-11-10"
    return m, rank([cand(**kw)], m)


ALL_CODES = ["over_budget", "limit_per_person", "appointment", "may_cover_more", "matches_like",
             "best_discount", "voucher_soon"]


# --- tests --------------------------------------------------------------------------------

def test_only_cites_shortlist():
    members, res = chicago()
    ids = {r.candidate.product_id for r in res.shortlist}
    titles = {r.candidate.product_id: r.candidate.title for r in res.shortlist}
    out = run_debate(res, members, seed=3, today=TODAY)
    assert out
    for ln in out:
        if ln.product_id is not None:
            assert ln.product_id in ids
            assert titles[ln.product_id] in ln.text
    # no title outside the shortlist ever appears
    for ln in out:
        assert "Escape Room Hour" not in ln.text or "p4" in ids


def test_private_member_numbers_hidden():
    m = [member("m0", "Ann", cap=4000, share=False), member("m1", "Bo", cap=100000)]
    res = rank([cand(price=5200, party=1, bound="exactly")], m)
    assert res.shortlist[0].verdicts[0].over_by_minor == 1200
    for seed in range(30):
        mine = lines_of(run_debate(res, m, seed, today=TODAY), "Ann")
        assert mine
        for ln in mine:
            for s in (fmt(4000), fmt(5200), fmt(1200)):
                assert s not in ln.text
            assert "$" not in ln.text


def test_shared_member_numbers_shown():
    m = [member("m0", "Ann", cap=4000, share=True), member("m1", "Bo", cap=100000)]
    res = rank([cand(price=5200, party=1, bound="exactly")], m)
    seen = set()
    for seed in range(30):
        for ln in lines_of(run_debate(res, m, seed, today=TODAY), "Ann"):
            seen.add("$12.00" in ln.text)
    assert True in seen
    # the other member's cap is never printed in Ann's lines
    for seed in range(30):
        for ln in lines_of(run_debate(res, m, seed, today=TODAY), "Ann"):
            assert fmt(100000) not in ln.text


def test_skip_line_names_no_member():
    members, res = chicago()
    res.excluded = [Excluded("x1", "Secret Dive Bar Crawl", "hard_no:bar"),
                    Excluded("x2", "Wine Tasting", "kids_21"),
                    Excluded("x3", "Group Trip", "quantity")]
    res.adult_dropped = 1
    for seed in range(20):
        out = run_debate(res, members, seed, today=TODAY)
        skip = [ln for ln in lines_of(out, "Scout") if "skipped" in ln.text.lower()]
        assert len(skip) == 1
        t = skip[0].text
        assert "4" in t
        for name in ("Ann", "Bo", "Cy"):
            assert name not in t
        for word in ("kids", "wine", "dive", "quantity"):
            assert word not in t.lower()
        assert "hard no" in t.lower()
        assert skip[0].product_id is None


def test_skip_line_singular_and_zero():
    members, res = chicago()
    res.excluded = [Excluded("x1", "T", "quantity")]
    res.adult_dropped = 0
    t = [ln.text for ln in run_debate(res, members, 1, today=TODAY) if "skipped" in ln.text.lower()][0]
    assert "1 offer " in t or t.endswith("1 offer")  # singular noun
    res.excluded = []
    out = run_debate(res, members, 1, today=TODAY)
    assert not any("0 offers" in ln.text for ln in out)


def test_deterministic_seed():
    members, res = chicago()
    a = run_debate(res, members, 7, today=TODAY)
    b = run_debate(res, members, 7, today=TODAY)
    assert a == b
    others = {tuple(texts(run_debate(res, members, s, today=TODAY))) for s in range(12)}
    assert len(others) > 1   # seeds produce varied conversations


def test_round_cap():
    members, res = chicago()
    for seed in range(40):
        rounds = run_debate_rounds(res, members, seed, today=TODAY)
        assert 1 <= len(rounds) <= 3
        for r in rounds:
            assert 1 <= len(r) <= len(members) + 2
    for n in (1, 2, 6):
        ms = [member(f"m{i}", f"P{i}", cap=2000 + 900 * i) for i in range(n)]
        res2 = rank([cand("a", price=4000), cand("b", title="Spa Hour", price=1500)], ms)
        for seed in range(10):
            rounds = run_debate_rounds(res2, ms, seed, today=TODAY)
            assert len(rounds) <= 3
            assert all(len(r) <= n + 2 for r in rounds)


def test_shared_fact_not_repeated_when_alternatives_exist():
    members, res = chicago()
    for seed in range(20):
        said = [(ln.product_id, c) for rnd in run_debate_traced(res, members, seed, today=TODAY)
                for ln, c in rnd if ln.kind == "advocate" and c not in ("over_budget", "matches_like")]
        assert len(said) == len(set(said)), (seed, said)


def test_each_advocate_speaks_once_per_round():
    members, res = chicago()
    rounds = run_debate_rounds(res, members, 4, today=TODAY)
    for r in rounds:
        speakers = [ln.speaker for ln in r if ln.kind == "advocate"]
        assert len(speakers) == len(set(speakers))


def test_every_fact_code_reachable():
    got = set()
    for code in ALL_CODES:
        m, res = scenario(code)
        for seed in range(6):
            for ln, c in sum(run_debate_traced(res, m, seed, today=TODAY), []):
                if ln.kind == "advocate" and c:
                    got.add(c)
                if c == code and ln.kind == "advocate":
                    assert ln.product_id == res.shortlist[0].candidate.product_id
    assert got >= set(ALL_CODES)


def test_strongest_fact_priority():
    # over budget beats everything else for the member who is over
    m = [member("m0", "Ann", cap=500, likes=["clay"]), member("m1", "Bo")]
    res = rank([cand(price=2000, original=4000, expires="2026-11-10",
                     facts=Facts(appointment=True, limit_per_person=1))], m)
    first = [c for ln, c in run_debate_traced(res, m, 1, today=TODAY)[1]
             if ln.speaker == "Ann"]
    assert first == ["over_budget"]


def test_voucher_soon_only_within_60_days():
    m = [member("m0", "Ann"), member("m1", "Bo")]
    far = rank([cand(expires="2027-03-01")], m)
    past = rank([cand(expires="2026-10-01")], m)
    junk = rank([cand(expires="soon")], m)
    for res in (far, past, junk):
        codes = [c for rnd in run_debate_traced(res, m, 1, today=TODAY) for _, c in rnd]
        assert "voucher_soon" not in codes


def test_no_banned_words_and_no_dashes():
    scen = [chicago()] + [scenario(c) for c in ALL_CODES]
    empty = rank([], [member("m0", "Ann")])
    empty.emptied_by = "budget"
    scen.append(([member("m0", "Ann")], empty))
    for members, res in scen:
        for seed in range(50):
            for ln in run_debate(res, members, seed, today=TODAY):
                low = ln.text.lower()
                for bad in BANNED:
                    assert bad not in low, (bad, ln.text)


def test_library_shape_and_hygiene():
    assert L.PERSONALITIES == ["blunt", "cheerful", "nerdy", "chill"]
    for code in ALL_CODES + ["fits_ok"]:
        for p in L.PERSONALITIES:
            pool = L.ADVOCATE[code][p]
            plain = [t for t in pool if "{over}" not in t and "{cap}" not in t]
            assert len(plain) >= 3, (code, p)
            if code == "over_budget":
                assert len(pool) - len(plain) >= 3
    every = list(_all_templates())
    for t in every:
        assert not re.search(r"\d", t.replace("{", "").replace("}", "")), t
        assert "$" not in t, t
        assert "—" not in t and "–" not in t
        for bad in BANNED:
            assert bad not in t.lower(), (bad, t)
        assert len(t) <= 110, (len(t), t)
    for name in ("Chicago", "Evanston", "Kiln"):
        assert not any(name in t for t in every)


def _all_templates():
    for code in L.ADVOCATE.values():
        for pool in code.values():
            yield from pool
    for pool in (L.SCOUT_NONE, L.SCOUT_ONE, L.SCOUT_MANY, L.SKIP_ZERO, L.SKIP_ONE, L.SKIP_MANY,
                 L.REFEREE_PICK_ONE, L.REFEREE_PICK_MANY, L.REFEREE_NONE):
        yield from pool
    for pool in L.REFEREE_EMPTY.values():
        yield from pool


def test_line_length_on_fixtures():
    members, res = chicago()
    for seed in range(30):
        for ln in run_debate(res, members, seed, today=TODAY):
            if ln.kind == "scout" and "1)" in ln.text:
                continue   # shortlist listing is bounded by real titles, not by template text
            assert len(ln.text) <= 140, ln.text


def test_scout_lists_every_shortlist_title():
    members, res = chicago()
    out = run_debate(res, members, 2, today=TODAY)
    listing = [ln for ln in out if ln.kind == "scout" and "1)" in ln.text][0]
    for r in res.shortlist:
        assert r.candidate.title in listing.text


def test_referee_pick_is_top():
    members, res = chicago()
    assert res.top
    out = run_debate(res, members, 5, today=TODAY)
    last = out[-1]
    assert last.kind == "referee"
    assert last.product_id == res.top[0].candidate.product_id
    assert res.top[0].candidate.title in last.text


def test_referee_emptied_by_names_constraint_and_offers_relax():
    m = [member("m0", "Ann")]
    cases = {"budget": "budget", "hard_no:bar": "hard no", "kids_21": "age", "quantity": "voucher"}
    for reason, needle in cases.items():
        res = rank([], m)
        res.emptied_by = reason
        out = run_debate(res, m, 3, today=TODAY)
        last = out[-1]
        assert last.kind == "referee" and last.product_id is None
        assert needle in last.text.lower()
        assert ("host" in last.text.lower()) == (reason in ("budget", "hard_no:bar"))
        assert "bar" not in last.text.lower().replace("budget", "")


def test_referee_empty_lines_match_relax_capabilities():
    """The app can relax the budget (host) and drop a hard no (its owner). It cannot relax age or quantity."""
    for t in L.REFEREE_EMPTY["hard_no"]:
        low = t.lower()
        assert "member who set that hard no can drop it" in low and "host can rerun" in low, t
        assert "relax" not in low, t
    for t in L.REFEREE_EMPTY["budget"]:
        assert "relax" in t.lower() and "host" in t.lower(), t
    for key in ("kids_21", "quantity"):
        for t in L.REFEREE_EMPTY[key]:
            low = t.lower()
            assert "relax" not in low and "rerun" not in low, t
            assert "another city or theme" in low, t
            assert "kid" not in low, t
def test_budget_emptied_still_has_advocates():
    members = [member("m0", "Ann", cap=1000), member("m1", "Bo", cap=1000)]
    res = rank([cand(price=9000)], members)
    assert res.emptied_by == "budget" and res.shortlist and not res.top
    out = run_debate(res, members, 2, today=TODAY)
    assert any(ln.kind == "advocate" for ln in out)
    assert out[-1].kind == "referee" and "relax" in out[-1].text.lower()


def test_no_candidates_at_all():
    res = rank([], [member("m0", "Ann")])
    out = run_debate(res, [member("m0", "Ann")], 1, today=TODAY)
    assert out[0].kind == "scout" and out[-1].kind == "referee"
    assert all(ln.kind != "advocate" for ln in out)


def test_line_roundtrip():
    ln = Line("Ann", "advocate", "hello", "p1")
    assert Line.from_dict(ln.to_dict()) == ln
    ln2 = Line("Scout", "scout", "hi", None)
    assert Line.from_dict(ln2.to_dict()) == ln2


def test_personality_by_member_index():
    # same fact, four members: each uses its own personality pool
    ms = [member(f"m{i}", f"P{i}", cap=100) for i in range(4)]
    res = rank([cand(price=9000, party=4, bound="exactly")], ms)
    title = res.shortlist[0].candidate.title
    for seed in range(10):
        for rnd in run_debate_traced(res, ms, seed, today=TODAY):
            for ln, code in rnd:
                if ln.kind != "advocate":
                    continue
                idx = int(ln.speaker[1:])
                pool = L.ADVOCATE[code][L.PERSONALITIES[idx % 4]]
                assert any(_matches(t, ln.text) for t in pool), ln.text
                other = L.ADVOCATE[code][L.PERSONALITIES[(idx + 1) % 4]]
                assert not any(_matches(t, ln.text) for t in other), ln.text
                assert title in ln.text


def _matches(template, text):
    pat = re.sub(r"\\\{\w+\\\}", ".+", re.escape(template))
    return re.fullmatch(pat, text) is not None


def test_no_like_line_after_over_budget_same_option():
    # Ann is over budget on the only offer AND likes it: she may say "over budget", never "I am in".
    m = [member("m0", "Ann", cap=500, likes=["clay"]), member("m1", "Bo")]
    res = rank([cand(title="Clay Wheel Night", price=2000, original=2500)], m)
    assert res.shortlist[0].verdicts[0].likes_matched and not res.shortlist[0].verdicts[0].fits_budget
    for seed in range(200):
        for rnd in run_debate_traced(res, m, seed, today=TODAY):
            for ln, code in rnd:
                if ln.speaker == "Ann":
                    assert code != "matches_like", (seed, ln.text)


def test_no_self_reference_by_name():
    members, res = chicago()
    scen = [(members, res)] + [scenario(c) for c in ALL_CODES]
    for ms, r in scen:
        for seed in range(100):
            for ln in run_debate(r, ms, seed, today=TODAY):
                if ln.kind == "advocate":
                    assert not re.search(r"\b" + re.escape(ln.speaker) + r"\b", ln.text), (seed, ln.text)


def test_may_cover_more_skipped_when_one_voucher():
    m = [member("m0", "Ann")]
    res = rank([cand(party=2, bound="up_to")], m)
    assert res.shortlist[0].quantity == 1
    codes = [c for rnd in run_debate_traced(res, m, 1, today=TODAY) for _, c in rnd]
    assert "may_cover_more" not in codes


def test_referee_at_least_when_top_is_full():
    ms = [member("m0", "Ann")]
    cands = [cand(f"p{i}", title=f"Offer {i}", price=1000 + i) for i in range(5)]
    res = rank(cands, ms)
    assert len(res.top) == 3
    for seed in range(10):
        assert "At least 3 options" in run_debate(res, ms, seed, today=TODAY)[-1].text
    res2 = rank(cands[:2], ms)
    assert len(res2.top) == 2
    for seed in range(10):
        t = run_debate(res2, ms, seed, today=TODAY)[-1].text
        assert "2 options" in t and "At least" not in t
