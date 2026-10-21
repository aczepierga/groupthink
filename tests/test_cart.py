"""Cart and order evidence: real LsClient over a fake Partner cart transport. No network, no real key."""
import csv
import html
import io
import json
import re
from datetime import timedelta
from urllib.parse import urlsplit

import pytest

from groupthink import config
from groupthink.ls_client import LsClient
from test_web import KEY, REQUIRED_COPY, T0, Env, FakeLs, argue, card, group, new_solo, p_hit

BUY_HOST = "checkout.groupon.test"
API_HOST = urlsplit(config.BASE_URL).netloc
ADMIN = "adm" + "-token-for-tests"
NO_LIMIT = ("Appointment required.",)


# ---- fake Partner cart API (behind the real LsClient) -------------------------------------------

def line_json(item, qty, price, available=True, reason=None):
    return {"id": item["optionId"], "productId": item["productId"], "optionId": item["optionId"],
            "title": "Deal", "optionTitle": "Standard", "media": [], "quantity": qty, "available": available,
            "unavailableReason": reason,
            "price": {"retail": price, "currency": "USD", "currencyPrecision": 2},
            "lineTotal": price * qty}


class CartApi:
    """Plays the Partner /carts endpoints. Records every request URL and every POST body."""

    def __init__(self, price=3000, env=None):
        self.price = price                 # the live retail price, minor units
        self.env = env
        self.carts, self.urls, self.posts = {}, [], []
        self.fail_create = None            # (status, body) returned for every POST /carts
        self.line_unavailable = False
        self.order_for = {}                # cart id -> grouponOrderUuid
        self.lock_prices = False           # True: a cart keeps the price it was made at
        self.price_after_creates = {}      # n carts created so far -> new live price

    def now(self):
        return self.env.clock[0] if self.env else T0

    def cart_body(self, cid):
        c = self.carts[cid]
        price = c["price"] if self.lock_prices else self.price
        items = [line_json(i, i["quantity"], price, not self.line_unavailable,
                           "SOLD_OUT" if self.line_unavailable else None) for i in c["items"]]
        total = sum(i["quantity"] * price for i in c["items"])
        return {"id": cid, "status": c["status"], "buyLink": "https://%s/cart/%s?utm_source=LS_AI&utm_campaign=sf1"
                % (BUY_HOST, cid), "country": "US", "currency": "USD", "clientReference": c["ref"],
                "itemCount": sum(i["quantity"] for i in c["items"]), "items": items,
                "totals": {"subtotal": total, "savings": 0, "grandTotal": total, "currency": "USD",
                           "currencyPrecision": 2},
                "messages": [], "createdAt": c["created"], "updatedAt": c["created"], "expiresAt": c["expires"],
                "grouponOrderUuid": self.order_for.get(cid)}

    def __call__(self, method, url, headers, body, timeout):
        self.urls.append((method, url))
        path = urlsplit(url).path
        if method == "POST" and path.endswith("/partner_storefront/carts"):
            req = json.loads(body)
            self.posts.append(req)
            if self.fail_create:
                return self.fail_create[0], {}, json.dumps(self.fail_create[1]).encode()
            item = req["items"][0]
            if item["expectedPrice"] != self.price:
                return 409, {}, json.dumps({"code": "aborted", "message": "Displayed price no longer valid",
                                            "details": {"currency": "USD", "currencyPrecision": 2,
                                                        "currentPrice": self.price, "error": "PRICE_MISMATCH",
                                                        "expectedPrice": item["expectedPrice"],
                                                        "path": "$.items[0]"}}).encode()
            cid = "cart-%d" % (len(self.carts) + 1)
            now = self.now()
            self.carts[cid] = {"status": "ACTIVE", "ref": req["clientReference"], "created": now.isoformat(),
                               "price": self.price,
                               "expires": (now + timedelta(days=1)).isoformat(),
                               "items": [{"productId": item["productId"], "optionId": item["optionId"],
                                          "quantity": item["quantity"]}]}
            body_out = json.dumps(self.cart_body(cid)).encode()
            self.price = self.price_after_creates.get(len(self.carts), self.price)
            return 200, {}, body_out
        if method == "GET" and "/partner_storefront/carts/" in path:
            cid = path.rsplit("/", 1)[1]
            if cid not in self.carts:
                return 404, {}, json.dumps({"code": "notFound", "message": "no such cart"}).encode()
            return 200, {}, json.dumps(self.cart_body(cid)).encode()
        return 500, {}, b"{}"


class CartLs(FakeLs):
    """FakeLs for the Scout; cart calls go through a real LsClient and the fake transport."""

    def __init__(self, api, **kw):
        super().__init__(**kw)
        self.api = api
        self.client = LsClient(transport=api, sleep=lambda s: None)

    def create_cart(self, items, client_reference):
        return self.client.create_cart(items, client_reference)

    def get_cart(self, cart_id):
        return self.client.get_cart(cart_id)


@pytest.fixture(autouse=True)
def _cart_env(monkeypatch):
    config._reset_cache()
    monkeypatch.setenv("IP_SALT", "test-salt-not-secret")
    monkeypatch.setenv("LS_PARTNER_KEY", KEY)
    for name in ("PROBES_ENABLED", "KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL",
                 "UPSTASH_REDIS_REST_TOKEN", "ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def make_env(fine=NO_LIMIT, price=3000, hits=None):
    api = CartApi(price)
    hits = hits or [p_hit("p1", "Pottery Wheel Night", price), p_hit("p2", "Axe Throwing Hour", price + 500),
            p_hit("p3", "Escape Room Hour", price + 1000)]       # cheapest first: p1 ranks first
    env = Env(ls=CartLs(api, hits=hits, fine=fine))
    api.env = env
    return env, api


def final_group(env, api, **group_kw):
    """A finished group: everyone voted for the top-ranked option. Returns (pid, host, members, ranked)."""
    pid, host, members = group(env, **group_kw)
    argue(host, pid)
    ranked = env.store.get_result(pid)[1].top[0]
    assert ranked.candidate.price_minor == api.price
    for b in [host, *members]:
        assert b.post(f"/p/{pid}/vote", data={"product_id": ranked.candidate.product_id}).status_code == 200
    return pid, host, members, ranked


def get_cart_link(c, pid, **form):
    return c.post(f"/p/{pid}/cart", data=form)


def checkout_hrefs(text):
    return [html.unescape(h) for h in re.findall(r'href="([^"]*%s[^"]*)"' % re.escape(BUY_HOST), text)]


# ---- quantity, limits ----------------------------------------------------------------------------

def test_quantity_sent_matches_rank():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    r = get_cart_link(host, pid)
    assert r.status_code == 200
    assert len(api.posts) == 1
    post = api.posts[0]
    assert post["country"] == "US" and post["clientReference"] == f"{pid}-1"
    assert post["items"] == [{"productId": ranked.candidate.product_id, "optionId": ranked.candidate.option_id,
                              "quantity": ranked.quantity, "expectedPrice": ranked.candidate.price_minor}]
    assert ranked.quantity == 3
    hrefs = checkout_hrefs(r.text)
    assert hrefs == ["https://%s/cart/cart-1?utm_source=LS_AI&utm_campaign=sf1" % BUY_HOST]
    assert "Checkout 1 of" not in r.text


def test_limit_creates_n_carts():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)
    assert ranked.quantity == 3
    r = get_cart_link(host, pid)
    assert r.status_code == 200
    assert [p["items"][0]["quantity"] for p in api.posts] == [1, 1, 1]
    assert [p["clientReference"] for p in api.posts] == [f"{pid}-1", f"{pid}-2", f"{pid}-3"]
    assert len(checkout_hrefs(r.text)) == 3
    for n in (1, 2, 3):
        assert f"Checkout {n} of 3" in r.text
    assert "3 separate checkouts" in r.text and "limit" in r.text.lower()
    assert len(env.store.carts_for(pid, ranked.candidate.option_id)) == 3


def test_limit_two_splits_quantity_without_exceeding_it():
    env, api = make_env(fine=("Limit 2 per person.",))
    pid, host, _, ranked = final_group(env, api)
    get_cart_link(host, pid)
    qtys = sorted(p["items"][0]["quantity"] for p in api.posts)
    assert qtys == [1, 2] and sum(qtys) == ranked.quantity


def test_cart_page_shows_quantity_unit_total_and_single_payer_line():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    t = get_cart_link(host, pid).text
    assert "3 vouchers" in t and "$30.00" in t and "$90.00" in t
    assert "One person pays on Groupon checkout" in t
    assert ranked.candidate.title in t


# ---- price and availability ----------------------------------------------------------------------

def test_price_mismatch_shows_new_price_no_link():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    api.price = 3100                                   # moved since search
    r = get_cart_link(host, pid)
    assert r.status_code == 200
    assert "Price changed to $31.00" in r.text
    assert checkout_hrefs(r.text) == [] and BUY_HOST not in r.text
    assert len(api.posts) == 1                          # never retried on its own
    assert env.store.carts_for(pid, ranked.candidate.option_id) == []
    assert 'name="expected_minor" value="3100"' in r.text
    # the host confirms the new price: one more attempt, at exactly that price
    r = get_cart_link(host, pid, product_id=ranked.candidate.product_id, expected_minor="3100")
    assert api.posts[1]["items"][0]["expectedPrice"] == 3100
    assert len(checkout_hrefs(r.text)) == 1 and "$31.00" in r.text and "$93.00" in r.text


def test_confirming_a_stale_price_is_refused_by_the_api_not_accepted():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    api.price = 3100
    r = get_cart_link(host, pid, expected_minor="2500")   # a forged or old confirm
    assert "Price changed to $31.00" in r.text and checkout_hrefs(r.text) == []


def test_reused_cart_whose_price_moved_asks_before_showing_the_link():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    get_cart_link(host, pid)
    api.price = 3300
    r = host.get(f"/p/{pid}/cart")
    assert "Price changed to $33.00" in r.text and checkout_hrefs(r.text) == []
    r = get_cart_link(host, pid, expected_minor="3300")
    assert len(checkout_hrefs(r.text)) == 1 and len(api.posts) == 1   # same cart, no new one


@pytest.mark.parametrize("how", ["not_cartable", "invalid_product", "line_unavailable"])
def test_not_cartable_offers_runner_up(how):
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    runner_up = env.store.get_result(pid)[1].top[1].candidate.title
    if how == "not_cartable":
        api.fail_create = (400, {"code": "failedPrecondition", "message": "nope",
                                 "details": {"error": "PRODUCT_NOT_CARTABLE"}})
    elif how == "invalid_product":
        api.fail_create = (404, {"code": "notFound", "message": "nope", "details": {"error": "INVALID_PRODUCT_ID"}})
    else:
        api.line_unavailable = True
    r = get_cart_link(host, pid)
    assert r.status_code == 200
    assert "No longer available" in r.text
    assert checkout_hrefs(r.text) == [] and BUY_HOST not in r.text
    assert runner_up in r.text and "Runner-up" in r.text


# ---- idempotence ---------------------------------------------------------------------------------

def test_second_click_reuses_cart():
    env, api = make_env()
    pid, host, members, ranked = final_group(env, api)
    first = checkout_hrefs(get_cart_link(host, pid).text)
    second = checkout_hrefs(get_cart_link(members[0], pid).text)      # another member presses it too
    assert first == second and len(first) == 1
    assert len(api.posts) == 1
    assert [m for m, u in api.urls if m == "GET"]                     # re-read through get_cart
    assert env.store.carts_for(pid, ranked.candidate.option_id) == ["cart-1"]


def test_second_click_reuses_every_cart_of_a_split_order():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, _ = final_group(env, api)
    get_cart_link(host, pid)
    r = get_cart_link(host, pid)
    assert len(api.posts) == 3 and len(checkout_hrefs(r.text)) == 3


def test_duplicate_recorded_ids_are_treated_as_a_set():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    get_cart_link(host, pid)
    env.store.record_cart(pid, ranked.candidate.product_id, ranked.candidate.option_id, "cart-1", "x")
    r = get_cart_link(host, pid)
    assert len(api.posts) == 1 and len(checkout_hrefs(r.text)) == 1


@pytest.mark.parametrize("how", ["expired", "emptied", "abandoned", "gone"])
def test_dead_carts_are_replaced(how):
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    get_cart_link(host, pid)
    c = api.carts["cart-1"]
    if how == "expired":
        c["expires"] = (T0 - timedelta(minutes=1)).isoformat()
    elif how == "emptied":
        c["items"] = []                                  # a GET after DELETE: 200 ACTIVE with no items
    elif how == "abandoned":
        c["status"] = "ABANDONED"
    else:
        del api.carts["cart-1"]                          # 404 on read
    r = get_cart_link(host, pid)
    assert len(api.posts) == 2
    assert checkout_hrefs(r.text) == ["https://%s/cart/cart-%d?utm_source=LS_AI&utm_campaign=sf1" % (
        BUY_HOST, 2 if how != "gone" else 1)]


def test_only_the_missing_carts_are_created():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)
    get_cart_link(host, pid)
    api.carts["cart-2"]["status"] = "EXPIRED"
    r = get_cart_link(host, pid)
    assert len(api.posts) == 4 and len(checkout_hrefs(r.text)) == 3


def test_get_cart_page_never_creates():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    r = host.get(f"/p/{pid}/cart")
    assert r.status_code == 200 and api.posts == []
    assert f'action="/p/{pid}/cart"' in r.text and "Get the cart link" in r.text
    get_cart_link(host, pid)
    r = host.get(f"/p/{pid}/cart")
    assert len(checkout_hrefs(r.text)) == 1 and len(api.posts) == 1


def test_buy_link_never_fetched():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, members, _ = final_group(env, api)
    get_cart_link(host, pid)
    get_cart_link(members[1], pid)
    host.get(f"/p/{pid}/cart")
    assert api.urls
    for _, url in api.urls:
        assert urlsplit(url).netloc == API_HOST, url
        assert BUY_HOST not in url


def test_links_open_in_new_tab_safely():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, _ = final_group(env, api)
    t = get_cart_link(host, pid).text
    assert t.count('target="_blank" rel="noopener noreferrer"') == 3


# ---- who can ask, and when -----------------------------------------------------------------------

def test_cart_needs_a_final_winner():
    env, api = make_env()
    pid, host, members = group(env)
    argue(host, pid)
    assert get_cart_link(host, pid).status_code == 409           # nobody has voted
    host.post(f"/p/{pid}/vote", data={"product_id": env.store.get_result(pid)[1].top[0].candidate.product_id})
    assert get_cart_link(host, pid).status_code == 409           # leading, not final
    assert host.get(f"/p/{pid}/cart").status_code == 409
    assert api.urls == []


def test_cart_refuses_another_product():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    other = env.store.get_result(pid)[1].top[1].candidate.product_id
    assert other != ranked.candidate.product_id
    assert get_cart_link(host, pid, product_id=other).status_code == 403
    assert get_cart_link(host, pid, product_id="zzz").status_code == 403
    assert api.posts == []


def test_cart_needs_host_or_member():
    env, api = make_env()
    pid, host, _, _ = final_group(env, api)
    stranger = env.browser()
    assert get_cart_link(stranger, pid).status_code == 403
    assert stranger.get(f"/p/{pid}/cart").status_code == 403
    assert api.urls == []


def test_cart_for_unknown_plan_is_410():
    env, _ = make_env()
    assert env.browser().post("/p/nope/cart").status_code == 410
    assert env.browser().get("/p/nope/cart").status_code == 410


def test_hosts_pick_can_be_carted():
    env, api = make_env()
    pid, host, members = group(env)
    argue(host, pid)
    a, b = [r.candidate.product_id for r in env.store.get_result(pid)[1].top[:2]]
    members[0].post(f"/p/{pid}/vote", data={"product_id": a})
    members[1].post(f"/p/{pid}/vote", data={"product_id": b})
    assert host.post(f"/p/{pid}/pick", data={"product_id": b}).status_code == 200
    r = get_cart_link(members[0], pid)
    assert r.status_code == 200 and api.posts[0]["items"][0]["productId"] == b


def test_argue_page_offers_cart_button_only_when_final():
    env, api = make_env()
    pid, host, members = group(env)
    argue(host, pid)
    assert "Get the cart link" not in host.get(f"/p/{pid}/argue").text
    ranked = env.store.get_result(pid)[1].top[0]
    for b in [host, *members]:
        b.post(f"/p/{pid}/vote", data={"product_id": ranked.candidate.product_id})
    assert f'action="/p/{pid}/cart"' in members[0].get(f"/p/{pid}/argue").text
    assert "Get the cart link" in host.get(f"/p/{pid}/argue").text
    assert "Get the cart link" not in env.browser().get(f"/p/{pid}/argue").text


# ---- copy and safety -----------------------------------------------------------------------------

def test_required_copy_on_cart_page():
    env, api = make_env()
    pid, host, _, _ = final_group(env, api)
    assert REQUIRED_COPY in host.get(f"/p/{pid}/cart").text
    assert REQUIRED_COPY in get_cart_link(host, pid).text
    env2, api2 = make_env()
    pid2, host2, _, _ = final_group(env2, api2)
    api2.price = 3100
    assert REQUIRED_COPY in get_cart_link(host2, pid2).text          # also on the price-changed screen


@pytest.mark.parametrize("state", ["ok", "mismatch", "gone", "empty"])
def test_cart_page_wording_and_dashes(state):
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, _ = final_group(env, api)
    if state == "mismatch":
        api.price = 3100
    if state == "gone":
        api.fail_create = (400, {"code": "failedPrecondition", "message": "x",
                                 "details": {"error": "PRODUCT_NOT_CARTABLE"}})
    t = (host.get(f"/p/{pid}/cart") if state == "empty" else get_cart_link(host, pid)).text.lower()
    for word in ("reserve", "reserved", "reservation", "reservations", "booked", "rating", "ratings", "review",
                 "reviews", "sold", "bestseller", "popular", "stars"):
        assert not re.search(r"\b%s\b" % word, t), word
    assert chr(0x2014) not in t and chr(0x2013) not in t


def test_non_https_buy_link_is_not_rendered():
    env, api = make_env()
    pid, host, _, _ = final_group(env, api)
    original = api.cart_body

    def bad(cid):
        b = original(cid)
        b["buyLink"] = "javascript:alert(1)"
        return b
    api.cart_body = bad
    t = get_cart_link(host, pid).text
    assert "javascript:" not in t


def test_upstream_failure_is_502_and_names_nothing():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    api.fail_create = (500, {"code": "internal", "message": "boom " + KEY})
    r = get_cart_link(host, pid)
    assert r.status_code == 502
    assert KEY not in r.text and "boom" not in r.text and checkout_hrefs(r.text) == []
    assert env.store.carts_for(pid, ranked.candidate.option_id) == []


def test_transient_read_failure_does_not_create_a_duplicate():
    env, api = make_env()
    pid, host, _, _ = final_group(env, api)
    get_cart_link(host, pid)
    real = api.__call__

    class Flaky:
        def __init__(self, inner):
            self.inner = inner

        def __call__(self, method, url, headers, body, timeout):
            if method == "GET":
                return 503, {}, b"{}"
            return self.inner(method, url, headers, body, timeout)
    env.ls.client = LsClient(transport=Flaky(real), sleep=lambda s: None)
    assert get_cart_link(host, pid).status_code == 502
    assert len(api.posts) == 1


# ---- order evidence ------------------------------------------------------------------------------

def test_admin_csv_requires_token(monkeypatch):
    env, api = make_env()
    pid, host, _, _ = final_group(env, api)
    get_cart_link(host, pid)
    c = env.browser()
    assert c.get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN}).status_code == 404   # ADMIN_TOKEN unset
    assert c.get("/admin/carts.csv").status_code == 404
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    assert c.get("/admin/carts.csv").status_code == 403
    assert c.get("/admin/carts.csv", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert c.get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN + "x"}).status_code == 403
    assert c.get("/admin/carts.csv?token=" + ADMIN).status_code == 403          # never from the URL
    assert c.cookies.get("X-Admin-Token") is None
    r = c.get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "no-store"


def test_admin_csv_rejects_non_ascii_token(monkeypatch):
    env, _ = make_env()
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    r = env.browser().get("/admin/carts.csv", headers={"X-Admin-Token": "café".encode("latin-1")})
    assert r.status_code == 403


def test_admin_csv_has_no_customer_fields(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, _ = final_group(env, api)
    get_cart_link(host, pid)
    api.order_for["cart-2"] = "11111111-2222-3333-4444-555555555555"
    api.carts["cart-2"]["status"] = "CHECKED_OUT"
    r = env.browser().get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN})
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["cart_id", "plan_id", "status", "has_order", "created_at", "demo"]
    body = {row[0]: row for row in rows[1:]}
    assert set(body) == {"cart-1", "cart-2", "cart-3"}
    assert body["cart-2"][1:4] == [pid, "CHECKED_OUT", "true"]
    assert body["cart-1"][1:4] == [pid, "ACTIVE", "false"]
    assert body["cart-1"][4] == T0.isoformat()
    for needle in ("11111111-2222", "buyLink", BUY_HOST, "http", "Ann", "Bo", "Cy", "utm_"):
        assert needle not in r.text, needle


def test_admin_csv_marks_unreadable_carts(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    env, api = make_env()
    pid, host, _, _ = final_group(env, api)
    get_cart_link(host, pid)
    del api.carts["cart-1"]
    r = env.browser().get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN})
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[1][0] == "cart-1" and rows[1][2] == "unreadable" and rows[1][3] == "unknown"


def test_admin_csv_empty_is_just_the_header(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    env, _ = make_env()
    r = env.browser().get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN})
    assert r.text.strip() == "cart_id,plan_id,status,has_order,created_at,demo"


# ---- fix round 1: ceiling, convergence, min units, accepted price, throttle ----------------------------

def ranked_for(quantity, limit, min_units=1):
    from groupthink.candidates import Candidate
    from groupthink.facts import Facts
    from groupthink.fit import Ranked
    cand = Candidate("p1", "o1", None, "Deal", "Std", None, "", "", 3000, None, 2, 1, "exactly", min_units, None,
                     None, [], [], None, None, Facts(limit_per_person=limit))
    return Ranked(cand, quantity, [], 0.0, None, [])


def test_chunks_respect_min_units():
    from groupthink.cart import checkouts_for
    assert checkouts_for(ranked_for(4, 3, min_units=2)) == (2, [2, 2])          # not [3, 1]
    assert checkouts_for(ranked_for(7, 3, min_units=2)) == (3, [3, 2, 2])
    assert checkouts_for(ranked_for(3, 1)) == (3, [1, 1, 1])
    assert checkouts_for(ranked_for(5, 9)) == (1, [5])                         # limit above the quantity
    assert checkouts_for(ranked_for(9, 2, min_units=2)) == (5, None)           # no split meets the minimum
    for qty, limit, mn in [(4, 3, 2), (7, 3, 2), (10, 4, 1), (8, 2, 2)]:
        n, chunks = checkouts_for(ranked_for(qty, limit, mn))
        assert chunks and sum(chunks) == qty and all(mn <= c <= limit for c in chunks)
    # and through the route: a group of 4, limit 3, minimum 2 -> two carts of 2
    hit = p_hit("p1", "Pottery Wheel Night", 3000)
    hit["options"][0]["minUnits"] = 2
    env, api = make_env(fine=("Limit 3 per person.",), hits=[hit])
    pid, host, _, ranked = final_group(env, api, size="4", others=(("Bo", "40"), ("Cy", "60"), ("Di", "50")))
    assert ranked.quantity == 4
    r = get_cart_link(host, pid)
    assert sorted(p["items"][0]["quantity"] for p in api.posts) == [2, 2]
    assert len(checkout_hrefs(r.text)) == 2


def test_impossible_split_message_no_api_call():
    hit = p_hit("p1", "Pottery Wheel Night", 3000)
    hit["options"][0]["minUnits"] = 2
    env, api = make_env(fine=("Limit 1 per person.",), hits=[hit])
    pid, host, _, ranked = final_group(env, api)         # 3 vouchers, one per checkout, minimum 2 each
    r = get_cart_link(host, pid)
    assert r.status_code == 200
    assert "cannot be split into checkouts that meet its minimum" in r.text
    assert checkout_hrefs(r.text) == [] and api.urls == [] and api.posts == []
    assert env.store.carts_for(pid, ranked.candidate.option_id) == []
    g = host.get(f"/p/{pid}/cart")
    assert "cannot be split" in g.text and api.urls == []


def test_cart_ceiling_refuses_without_api_call():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)         # 3 checkouts -> ceiling 9
    assert env.store.claim_cart_creates(pid, 8, 9)       # other requests already hold 8 of the 9
    r = get_cart_link(host, pid)
    assert r.status_code == 429
    assert "already being prepared" in r.text and "Refresh in a few seconds" in r.text
    assert api.urls == [] and api.posts == []
    assert checkout_hrefs(r.text) == []


def test_parallel_cart_posts_capped():
    """Two requests that both read zero existing carts (the second runs inside the first's create call):
    the store ceiling stops the second before it calls the API."""
    from groupthink.cart import CartBusy, make_carts
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, ranked = final_group(env, api)
    client = env.ls.client
    ceiling = 9
    assert env.store.claim_cart_creates(pid, 6, ceiling)          # the ceiling edge: room for exactly one batch
    outcomes = []
    real_create = client.create_cart

    def racing_create(items, ref):
        if not outcomes:
            outcomes.append("nested")
            try:
                make_carts(client, env.store, ranked, pid, now=T0)
                outcomes.append("second ran")
            except CartBusy:
                outcomes.append("second busy")
        return real_create(items, ref)
    client.create_cart = racing_create
    view = make_carts(client, env.store, ranked, pid, now=T0)
    assert outcomes == ["nested", "second busy"]
    assert len(api.posts) == 3 and len(view.links) == 3
    # claimed total never passes the ceiling: one more claim of any size is refused
    assert not env.store.claim_cart_creates(pid, 1, ceiling)


def test_failed_create_gives_the_claim_back():
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, _ = final_group(env, api)
    api.price = 3100
    for _ in range(6):                                   # six price-changed screens in a row
        assert "Price changed" in get_cart_link(host, pid).text
    assert get_cart_link(host, pid).status_code == 200   # never ran into the ceiling


def test_mid_split_price_change_converges():
    env, api = make_env(fine=("Limit 1 per person.",))
    api.lock_prices = True
    api.price_after_creates = {1: 3100}                  # the price moves right after the first cart
    pid, host, _, ranked = final_group(env, api)
    api.price = 3000
    r = get_cart_link(host, pid)
    assert "Price changed to $31.00" in r.text and checkout_hrefs(r.text) == []
    assert api.carts["cart-1"]["price"] == 3000          # one cart exists at the old price
    r = get_cart_link(host, pid, product_id=ranked.candidate.product_id, expected_minor="3100")
    links = checkout_hrefs(r.text)
    assert len(links) == 3 and "Price changed" not in r.text
    assert not any("/cart/cart-1?" in link for link in links)         # the old-price cart is not reused
    shown = {link.split("/cart/")[1].split("?")[0] for link in links}
    assert all(api.carts[cid]["price"] == 3100 for cid in shown)
    assert "$31.00" in r.text and "$93.00" in r.text
    # nothing flips back on the next read
    again = host.get(f"/p/{pid}/cart")
    assert len(checkout_hrefs(again.text)) == 3 and "Price changed" not in again.text


def test_get_after_confirm_shows_links():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    api.price = 3100
    get_cart_link(host, pid)
    r = get_cart_link(host, pid, expected_minor="3100")
    assert len(checkout_hrefs(r.text)) == 1
    assert env.store.get_accepted_price(pid, ranked.candidate.option_id) == 3100
    g = host.get(f"/p/{pid}/cart")                       # no price in the request: the accepted one is used
    assert len(checkout_hrefs(g.text)) == 1 and "$31.00" in g.text and "Price changed" not in g.text
    again = get_cart_link(host, pid)                     # a plain second click reuses it too
    assert len(checkout_hrefs(again.text)) == 1 and len(api.posts) == 2


def test_confirmed_price_is_bounded_to_twice_the_voted_price():
    env, api = make_env()
    pid, host, _, ranked = final_group(env, api)
    for bad in ("6001", "0", "-5", "abc", "99999999"):
        r = get_cart_link(host, pid, expected_minor=bad)
        assert r.status_code == 400, bad
    assert api.urls == []
    api.price = 6000
    r = get_cart_link(host, pid, expected_minor="6000")  # exactly 2x is allowed
    assert r.status_code == 200 and len(checkout_hrefs(r.text)) == 1


def test_admin_route_throttles_wrong_tokens(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    env, _ = make_env()
    c = env.browser()
    ip = {"x-real-ip": "198.51.100.7"}
    for _ in range(5):
        assert c.get("/admin/carts.csv", headers=dict(ip, **{"X-Admin-Token": "wrong"})).status_code == 403
    assert c.get("/admin/carts.csv", headers=dict(ip, **{"X-Admin-Token": "wrong"})).status_code == 429
    assert c.get("/admin/carts.csv", headers=dict(ip, **{"X-Admin-Token": ADMIN})).status_code == 429
    other = {"x-real-ip": "198.51.100.8"}
    assert c.get("/admin/carts.csv", headers=dict(other, **{"X-Admin-Token": ADMIN})).status_code == 200
    env.advance(hours=1)                                  # a new hour, a new allowance
    assert c.get("/admin/carts.csv", headers=dict(ip, **{"X-Admin-Token": ADMIN})).status_code == 200


# ---- final review wave: demo carts are real, CSV hygiene -----------------------------------------------

DEMO_NOTICE = "This is a demo plan with sample people. The cart is real: buy only if you actually want this deal."


def demo_cart(env, api):
    """A demo plan whose only voter (the visitor) voted for the top option. Returns (browser, pid)."""
    c = env.browser()
    r = c.post("/demo", data={"city": "Chicago"}, headers={"x-real-ip": "203.0.113.50"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    pid = r.headers["location"].rsplit("/", 1)[1]
    argue(c, pid)
    top = env.store.get_result(pid)[1].top[0]
    assert c.post(f"/p/{pid}/vote", data={"product_id": top.candidate.product_id}).status_code == 200
    return c, pid


def test_demo_cart_page_shows_demo_notice():
    env, api = make_env()
    c, pid = demo_cart(env, api)
    assert env.store.get_plan(pid)["demo"] is True
    for r in (c.get(f"/p/{pid}/cart"), get_cart_link(c, pid)):
        assert r.status_code == 200
        assert DEMO_NOTICE in r.text
    # a real plan carries no such notice, and its flag is stored as False
    pid2, host, _, _ = final_group(env, api)
    assert env.store.get_plan(pid2)["demo"] is False
    assert DEMO_NOTICE not in get_cart_link(host, pid2).text


def test_admin_csv_has_demo_column(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    env, api = make_env()
    c, demo_pid = demo_cart(env, api)
    get_cart_link(c, demo_pid)
    real_pid, host, _, _ = final_group(env, api)
    get_cart_link(host, real_pid)
    r = env.browser().get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN})
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][-1] == "demo" and len(rows[0]) == 6
    by_plan = {row[1]: row[5] for row in rows[1:]}
    assert by_plan == {demo_pid: "true", real_pid: "false"}


def test_admin_csv_dedupes_cart_ids(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    env, api = make_env(fine=("Limit 1 per person.",))
    pid, host, _, _ = final_group(env, api)
    get_cart_link(host, pid)
    real = env.store.carts

    def scan_repeats():
        rows = real()
        return rows + rows[:2]            # a SCAN may return a key more than once
    env.store.carts = scan_repeats
    r = env.browser().get("/admin/carts.csv", headers={"X-Admin-Token": ADMIN})
    ids = [row[0] for row in list(csv.reader(io.StringIO(r.text)))[1:]]
    assert sorted(ids) == ["cart-1", "cart-2", "cart-3"]


# ---- solo plans ----------------------------------------------------------------------------------

def solo_run(env):
    c = env.browser()
    pid = new_solo(c)
    card(c, pid, "Ann", "50")
    argue(c, pid)
    return pid, c, [r.candidate.product_id for r in env.store.get_result(pid)[1].top]


def test_solo_pick_another_then_cart():
    env, api = make_env()
    pid, c, ids = solo_run(env)
    assert len(ids) >= 2
    r = c.post(f"/p/{pid}/pick", data={"product_id": ids[1]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/p/{pid}/argue"
    page = c.get(f"/p/{pid}/argue").text
    assert re.search(r'data-winner="%s"' % re.escape(ids[1]), page)
    r = get_cart_link(c, pid)
    assert r.status_code == 200
    assert len(api.posts) == 1
    item = api.posts[0]["items"][0]
    assert item["productId"] == ids[1] and item["quantity"] == 1
    assert item["expectedPrice"] == env.store.get_result(pid)[1].top[1].candidate.price_minor


def test_solo_pick_rejects_non_top_option():
    env, api = make_env()
    pid, c, ids = solo_run(env)
    assert c.post(f"/p/{pid}/pick", data={"product_id": "zzz"}).status_code == 400
    assert c.post(f"/p/{pid}/pick", data={}).status_code == 400
    assert env.store.get_pick(pid) is None
    assert env.browser().post(f"/p/{pid}/pick", data={"product_id": ids[1]}).status_code == 403


def test_solo_cart_page_needs_no_votes():
    env, api = make_env()
    pid, c, ids = solo_run(env)
    assert env.store.votes(pid) == {}
    r = get_cart_link(c, pid)
    assert r.status_code == 200
    assert api.posts[0]["items"][0]["productId"] == ids[0]
    assert REQUIRED_COPY in c.get(f"/p/{pid}/cart").text
    assert env.browser().post(f"/p/{pid}/cart", data={}).status_code == 403


SOLO_WORDS = re.compile(r"group|everyone|vote|\bhost\b|someone")


def solo_words(page):
    body = re.sub(r"<[^>]+>", " ", page).replace("GroupThink", "").replace("Groupon", "").lower()
    return SOLO_WORDS.search(body)


def test_solo_cart_page_has_no_group_words():
    env, api = make_env()
    pid, c, _ = solo_run(env)
    assert not solo_words(get_cart_link(c, pid).text)
    api.price = 3100
    mismatch = get_cart_link(c, pid).text
    assert "Price changed" in mismatch and not solo_words(mismatch)
    assert "you agree to the new price" in mismatch


@pytest.mark.parametrize("how", ["not_cartable", "line_unavailable"])
def test_solo_unavailable_runner_up_has_no_group_words(how):
    env, api = make_env()
    pid, c, _ = solo_run(env)
    if how == "not_cartable":
        api.fail_create = (400, {"code": "failedPrecondition", "message": "nope",
                                 "details": {"error": "PRODUCT_NOT_CARTABLE"}})
    else:
        api.line_unavailable = True
    r = get_cart_link(c, pid)
    assert "No longer available" in r.text and "Runner-up" in r.text and "choose it" in r.text
    assert not solo_words(r.text)


def test_solo_error_states_have_no_group_words():
    api = CartApi(3000)
    env = Env(ls=CartLs(api, hits=[p_hit("p1", "Pottery Wheel Night", 3000)]))
    api.env = env
    pid, c, _ = solo_run(env)
    api.fail_create = (500, {})
    assert not solo_words(get_cart_link(c, pid).text)


def test_impossible_split_message_has_solo_variant_and_group_text_unchanged():
    from groupthink.cart import make_carts
    ranked = ranked_for(3, 1, min_units=2)       # not reachable at solo quantity 1, but worded anyway
    group = make_carts(None, None, ranked, "p").error
    solo = make_carts(None, None, ranked, "p", solo=True).error
    assert group.endswith("Ask the group to vote for another option.")
    assert "choose another option" in solo and not solo_words(solo)


def test_plain_message_group_unchanged_and_solo_variant():
    from groupthink.cart import _plain_message
    ranked = ranked_for(3, 2)
    assert _plain_message(ranked, 2) == (
        "Your group needs 3 vouchers and the fine print limits each checkout to 2 per person, so this "
        "takes 2 separate checkouts. One person pays on each.")
    assert _plain_message(ranked, 2, solo=False) == _plain_message(ranked, 2)
    solo = _plain_message(ranked, 2, solo=True)
    assert solo == ("You need 3 vouchers and the fine print limits each checkout to 2 per person, so this "
                    "takes 2 separate checkouts. You pay on each.")
    assert not solo_words(solo)
    assert _plain_message(ranked, 1, solo=True) is None
