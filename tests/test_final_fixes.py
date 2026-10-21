"""Final fix wave: winner money, solo quantity, solo copy, solo seat, not-cartable only when nothing was carted."""
import json
import re

from groupthink.fit import rank
from groupthink.ls_client import LsClient
from groupthink.cart import make_carts
from test_cart import (NOT_CARTABLE, T0, CartApi, _cart_env, final_group, get_cart_link, make_env, solo_run,
                       solo_words)
from test_fit import cand, group as fit_group
from test_web import (Env, _env, argue, card, group, new_plan, new_solo, no_group_words, p_hit)  # noqa: F401


# ---- 1. winner box shows the money facts ---------------------------------------------------------

def winner_box(page):
    return re.search(r'<p class="winner".*?</p>\s*(?:<p class="muted money">.*?</p>)?', page, re.S).group(0)


def test_solo_winner_box_shows_voucher_count_and_total():
    env, api = make_env()
    pid, c, _ = solo_run(env)
    box = winner_box(c.get(f"/p/{pid}/argue").text)
    assert "1 voucher, $30.00 total" in box
    assert not solo_words(box)


def test_group_winner_box_shows_voucher_count_and_total():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    assert ranked.quantity == 3
    assert "3 vouchers, $90.00 total" in winner_box(host.get(f"/p/{pid}/argue").text)


# ---- 2. solo quantity is always 1 ----------------------------------------------------------------

def test_solo_rank_drops_offers_with_a_minimum_above_one():
    cands = [cand("p1", "o1", "Needs two", price=3000, min_units=2), cand("p2", "o2", "Plain", price=3500)]
    res = rank(cands, fit_group(1), solo=True)
    assert [r.candidate.product_id for r in res.top] == ["p2"]
    assert all(r.quantity == 1 for r in res.top + res.shortlist)
    assert [(e.product_id, e.reason) for e in res.excluded] == [("p1", "quantity")]


def test_solo_rank_all_minimums_above_one_is_a_quantity_empty():
    res = rank([cand(min_units=2)], fit_group(1), solo=True)
    assert res.top == [] and res.emptied_by == "quantity"


def test_group_rank_keeps_offers_with_a_minimum_above_one():
    res = rank([cand("p1", "o1", "Needs two", price=3000, min_units=2)], fit_group(1))
    assert [r.candidate.product_id for r in res.top] == ["p1"] and res.top[0].quantity == 2


def two_min_hit():
    h = p_hit("p1", "Pottery Wheel Night", 3000)
    h["options"][0]["minUnits"] = 2
    return h


def test_solo_run_never_lists_an_offer_with_a_minimum_above_one():
    env, api = make_env(hits=[two_min_hit(), p_hit("p2", "Axe Throwing Hour", 3500)])
    pid, c, ids = solo_run(env)
    assert ids == ["p2"]
    result = env.store.get_result(pid)[1]
    assert all(r.quantity == 1 for r in result.top + result.shortlist)
    assert "Pottery Wheel Night" not in c.get(f"/p/{pid}/argue").text


def test_group_run_still_lists_an_offer_with_a_minimum_above_one():
    env, api = make_env(hits=[two_min_hit()])
    pid, host, _ = group(env, size="2", others=(("Bo", "40"),))
    argue(host, pid)
    assert [r.candidate.product_id for r in env.store.get_result(pid)[1].top] == ["p1"]


# ---- 3. solo copy --------------------------------------------------------------------------------

def test_new_plan_page_does_not_promise_a_share_link_to_solo_players():
    t = Env().browser().get("/").text
    assert "You get a link to share" not in t
    assert "Group plans give you a link to share" in t


def test_card_page_per_person_wording_only_for_groups():
    env = Env()
    c = env.browser()
    solo_card = c.get(f"/p/{new_solo(c)}/card").text
    assert "per person" not in solo_card and "Max you will spend, in dollars" in solo_card
    no_group_words(solo_card)
    host = env.browser()
    assert "Max you will spend per person, in dollars" in host.get(f"/p/{new_plan(host)}/card").text


# ---- 4. the solo seat belongs to the host --------------------------------------------------------

def test_solo_seat_cannot_be_filled_by_a_non_host_before_the_host_saves():
    env = Env()
    host = env.browser()
    pid = new_solo(host)
    stranger = env.browser()
    r = stranger.get(f"/p/{pid}/card")
    assert r.status_code == 409 and "full" in r.text.lower()
    r = card(stranger, pid, "Mallory", expect=409)
    assert "full" in r.text.lower()
    assert env.store.cards(pid) == []
    card(host, pid, "Ann")
    assert [m.name for m in env.store.cards(pid)] == ["Ann"]


def test_group_seat_is_still_open_to_non_hosts():
    env = Env()
    pid = new_plan(env.browser())
    card(env.browser(), pid, "Bo")
    assert [m.name for m in env.store.cards(pid)] == ["Bo"]


# ---- 5. not-cartable mark only when nothing was carted --------------------------------------------

def _fail_nth_create(api, n):
    """A transport that behaves like `api` but answers PRODUCT_NOT_CARTABLE to the n-th cart create."""
    state = {"n": 0}

    def flaky(method, url, headers, body, timeout):
        if method == "POST" and url.endswith("/partner_storefront/carts"):
            state["n"] += 1
            if state["n"] == n:
                return 400, {}, json.dumps(NOT_CARTABLE[1]).encode()
        return api(method, url, headers, body, timeout)
    return flaky


def test_not_cartable_on_a_later_chunk_does_not_mark_or_move_on():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)                 # three checkouts
    env.ls.client = LsClient(transport=_fail_nth_create(api, 2), sleep=lambda s: None)
    r = get_cart_link(host, pid)
    pids = env.store.not_cartable_ids(env.clock[0])
    assert len(env.store.carts_for(pid, ranked.candidate.option_id)) == 1      # chunk 1 was made
    assert ranked.candidate.product_id not in pids
    assert "first pick" not in r.text                                          # the plan did not move on
    assert ranked.candidate.title in r.text


def test_not_cartable_on_the_first_chunk_still_marks_and_moves_on():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)
    env.ls.client = LsClient(transport=_fail_nth_create(api, 1), sleep=lambda s: None)
    r = get_cart_link(host, pid)
    assert ranked.candidate.product_id in env.store.not_cartable_ids(env.clock[0])
    assert "first pick" in r.text


def test_make_carts_flags_not_cartable_only_with_no_cart_made():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)
    env.ls.client = LsClient(transport=_fail_nth_create(api, 2), sleep=lambda s: None)
    later = make_carts(env.ls, env.store, ranked, pid, now=T0)
    assert later.unavailable is True and later.not_cartable is False
    env2, api2 = make_env(fine=("Limit 1 per person.",))
    pid2, _, _, ranked2 = final_group(env2, api2)
    env2.ls.client = LsClient(transport=_fail_nth_create(api2, 1), sleep=lambda s: None)
    first = make_carts(env2.ls, env2.store, ranked2, pid2, now=T0)
    assert first.unavailable is True and first.not_cartable is True
