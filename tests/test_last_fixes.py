"""Last fix wave: one empty-search message, hard no's keep their empty reason, refund note for every empty kind,
referee lines for a plan whose city cannot change."""
import re

from groupthink import lines as L
from groupthink.fit import rank
from test_cart import SOLO_WORDS
from test_fit import cand, group as fit_group
from test_web import (Env, FakeLs, _env, argue, card, group, new_plan, p_hit)  # noqa: F401


# ---- 1. one empty-search message ---------------------------------------------------------------

def test_partner_empty_shows_one_callout_not_two():
    env = Env(ls=FakeLs(hits=[]))
    pid, host, _ = group(env)
    argue(host, pid)
    t = host.get(f"/p/{pid}/argue").text
    assert "Raise a budget or try a nearby city." in t
    assert t.count("Raise a budget or try a nearby city.") == 1
    assert "Live search found no offers" not in t
    assert t.count('>Naperville</a>') == 1 and t.count('>Schaumburg</a>') == 1


# ---- 2. hard no's keep the empty reason --------------------------------------------------------

def hard_no_mix_env():
    hits = [p_hit("s1", "Spa Day One", 3000), p_hit("s2", "Spa Day Two", 3100),
            p_hit("t1", "City Tour", 2500),
            p_hit("y1", "Kids Party One", 2000), p_hit("y2", "Kids Party Two", 2100),
            p_hit("y3", "Kids Party Three", 2200)]
    env = Env(ls=FakeLs(hits=hits))
    host = env.browser()
    pid = new_plan(host, group_size="2")
    card(host, pid, "Ann", "50", hard_nos="spa")
    bo = env.browser()
    card(bo, pid, "Bo", "50", hard_nos="tour")
    argue(host, pid)
    return env, pid, host, bo


def test_hard_no_bucket_wins_over_a_bigger_single_reason():
    env, pid, host, bo = hard_no_mix_env()
    r = env.store.get_result(pid)[1]
    assert not r.shortlist and (r.emptied_by or "").startswith("hard_no:")
    assert r.emptied_by == "hard_no:spa"


def test_hard_no_bucket_shows_each_owner_their_drop_form():
    env, pid, host, bo = hard_no_mix_env()
    for who, mine, other in ((host, "spa", "tour"), (bo, "tour", "spa")):
        for url in (f"/p/{pid}/argue", f"/p/{pid}/card"):
            t = who.get(url).text
            assert f'value="hard_no:{mine}"' in t and "Drop this hard no" in t
            assert f'value="hard_no:{other}"' not in t
    stranger = env.browser().get(f"/p/{pid}/argue").text
    assert "Hard no's on 2 cards ruled every offer out." in stranger
    assert not re.search(r"\b(spa|tour)\b", stranger.lower())
    assert "Drop this hard no" not in stranger


def test_rank_hard_no_ties_go_to_the_first_member_with_a_word():
    cands = [cand("p1", "o1", "Tour Day", price=3000), cand("p2", "o2", "Spa Day", price=3000),
             cand("p3", "o3", "Kids Party", price=3000)]
    members = fit_group(2)
    members[0].hard_nos = ["spa"]
    members[1].hard_nos = ["tour"]
    res = rank(cands, members)
    assert res.emptied_by == "hard_no:spa"


# ---- 3. refund note for every empty kind -------------------------------------------------------

def test_refunded_hard_no_run_shows_the_refund_note():
    env = Env()
    pid, host, _ = group(env, hard_nos="pottery, axe, escape")
    argue(host, pid)
    r = env.store.get_result(pid)[1]
    assert r.refunded is True
    assert "This search did not use up a run." in host.get(f"/p/{pid}/argue").text


def test_unrefunded_hard_no_run_has_no_refund_note():
    env = Env()
    pid, host, _ = group(env, hard_nos="pottery, axe, escape")
    for _ in range(3):
        argue(host, pid)
    r = env.store.get_result(pid)[1]
    assert r.refunded is False
    assert "This search did not use up a run." not in host.get(f"/p/{pid}/argue").text


# ---- 4. referee lines never suggest changing a plan's city -------------------------------------

def test_away_and_budget_cap_lines_do_not_offer_a_rerun_in_a_nearby_city():
    for pools in (L.REFEREE_EMPTY, L.SOLO_REFEREE_EMPTY):
        for key in ("away", "budget_cap"):
            for line in pools[key]:
                assert not ("rerun" in line and "nearby city" in line), line
                assert "new plan" in line.lower(), line
                assert not any(ch in line for ch in (chr(0x2013), chr(0x2014)))
    for key in ("away", "budget_cap"):
        for line in L.SOLO_REFEREE_EMPTY[key]:
            assert not SOLO_WORDS.search(line.lower()), line
