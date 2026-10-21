import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from groupthink.candidates import Candidate
from groupthink.debate import Line
from groupthink.facts import Facts
from groupthink.fit import Member, RankResult, rank
from groupthink.kv import Kv
from groupthink.store import PlanNotFound, SqliteStore, hash_ip
from groupthink.store_durable import DurableStore
from tests.fake_upstash import FakeUpstash

DAY = 86400


class Backend:
    """store + reopen() + age(days): same suite, three backends."""

    def __init__(self, kind, tmp_path):
        self.kind, self.offset = kind, timedelta(0)
        self.fake = FakeUpstash() if kind == "durable" else None
        self.path = str(tmp_path / "s.db") if kind == "sqlite" else None
        self.store = self.reopen()

    def reopen(self):
        if self.kind == "sqlite":
            self.store = SqliteStore(self.path, now=lambda: datetime.now(timezone.utc) + self.offset)
        elif self.kind == "durable":
            self.store = DurableStore(Kv("https://fake.upstash.io", "tok" + "_fake", transport=self.fake))
        else:
            self.store = DurableStore(Kv.from_env())
        return self.store

    def age(self, days):
        self.offset += timedelta(days=days)
        if self.fake:
            self.fake.advance(days * DAY)

    @property
    def expires_to_none(self):
        return self.kind != "sqlite"


@pytest.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("durable", id="durable", marks=pytest.mark.durable),
    pytest.param("live", id="live", marks=[pytest.mark.live, pytest.mark.skipif(
        not os.environ.get("KV_REST_API_URL"), reason="KV_REST_API_URL not set")]),
])
def be(request, tmp_path):
    return Backend(request.param, tmp_path)


def now():
    return datetime.now(timezone.utc)


def plan(**kw):
    p = {"city": "Chicago", "date_text": "Sat Nov 8", "group_size": 3, "theme": "date night",
         "host_token_hash": "h" * 64, "created_at": now().isoformat()}
    p.update(kw)
    return p


def mem(mid, name="Ann", cap=5000, likes=None):
    return Member(id=mid, name=name, max_budget_minor=cap, likes=likes or ["pottery"], hard_nos=[],
                  kids=False, constraints="", share_numbers=True)


def cand(pid="p1", oid="o1", title="Clay Wheel Night"):
    return Candidate(
        product_id=pid, option_id=oid, permalink=None, title=title, option_title="", merchant=None,
        place="", city="Chicago", price_minor=3000, original_minor=None, precision=2,
        party_size=1, party_bound="exactly", min_units=1, max_units=None,
        fine_print=None, highlights=[], categories=[], voucher_expires=None, promo_code=None,
        facts=Facts())


def result():
    return rank([cand("p1", "o1"), cand("p2", "o2", "Glass Blowing")], [mem("m1"), mem("m2", "Bo")])


def test_create_and_get_plan(be):
    s = be.store
    pid = s.create_plan(plan())
    assert len(pid) == 16 and pid.replace("-", "").replace("_", "").isalnum()
    got = s.get_plan(pid)
    assert got["city"] == "Chicago" and got["group_size"] == 3 and got["host_token_hash"] == "h" * 64
    assert s.get_plan("nope" * 4) is None


def test_round_trip_result(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.get_result(pid) is None and s.argue_count(pid) == 0
    res = result()
    lines = [Line("Scout", "scout", "Found 2.", None), Line("Ann", "advocate", "Pick p1.", "p1")]
    s.save_result(pid, lines, res)
    got_lines, got_res = s.get_result(pid)
    assert got_lines == lines
    assert isinstance(got_res, RankResult) and got_res.to_dict() == res.to_dict()
    assert s.argue_count(pid) == 0           # saving a result does not count a run; claim_run does
    assert s.claim_run(pid) == 1 and s.claim_run(pid) == 2
    s.save_result(pid, lines, res)
    assert s.argue_count(pid) == 2


def test_claim_run_is_capped(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.claim_run(pid, 2) == 1
    # two more claims at runs=1: exactly one succeeds
    got = [s.claim_run(pid, 2), s.claim_run(pid, 2)]
    assert sorted(g is None for g in got) == [False, True] and 2 in got
    assert s.argue_count(pid) == 2
    assert s.claim_run(pid, 2) is None and s.argue_count(pid) == 2


def test_release_run_gives_the_claim_back(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.claim_run(pid, 2) == 1 and s.claim_run(pid, 2) == 2 and s.claim_run(pid, 2) is None
    s.release_run(pid)
    assert s.argue_count(pid) == 1
    assert s.claim_run(pid, 2) == 2
    s.release_run(pid)
    s.release_run(pid)
    s.release_run(pid)                          # never below zero
    assert s.argue_count(pid) == 0


def test_claim_run_on_missing_plan_raises(be):
    with pytest.raises(PlanNotFound):
        be.store.claim_run("gone0000gone0000", 2)


def test_rerun_clears_votes_and_pick(be):
    s = be.store
    pid = s.create_plan(plan())
    s.save_result(pid, [Line("Scout", "scout", "x", None)], result())
    s.put_vote(pid, "m1", "p1")
    s.put_vote(pid, "m2", "p2")
    s.set_pick(pid, "p1")
    assert s.votes(pid) == {"p1": 1, "p2": 1} and s.get_pick(pid) == "p1"
    s.save_result(pid, [Line("Scout", "scout", "y", None)], result())
    assert s.votes(pid) == {} and s.get_pick(pid) is None
    assert s.get_result(pid)[0][0].text == "y"
    s.put_vote(pid, "m1", "p1")                 # voting works again afterwards
    assert s.votes(pid) == {"p1": 1}


def test_put_card_cap_refuses_new_member_only(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.put_card(pid, mem("m1"), cap=2) is True
    assert s.put_card(pid, mem("m2"), cap=2) is True
    assert s.put_card(pid, mem("m3"), cap=2) is False
    assert s.put_card(pid, mem("m1", cap=7000), cap=2) is True      # editing an existing card is fine
    assert {m.id: m.max_budget_minor for m in s.cards(pid)} == {"m1": 7000, "m2": 5000}


def test_card_overwrite_same_member(be):
    s = be.store
    pid = s.create_plan(plan())
    s.put_card(pid, mem("m1", "Ann", cap=1000))
    s.put_card(pid, mem("m2", "Bo"))
    s.put_card(pid, mem("m1", "Ann", cap=9999))
    cards = {m.id: m for m in s.cards(pid)}
    assert set(cards) == {"m1", "m2"} and cards["m1"].max_budget_minor == 9999
    assert isinstance(cards["m2"], Member)
    assert s.cards("unknownplan0000x") == []


def test_votes_tally(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.votes(pid) == {} and s.get_pick(pid) is None
    s.put_vote(pid, "m1", "p1")
    s.put_vote(pid, "m2", "p1")
    s.put_vote(pid, "m3", "p2")
    s.put_vote(pid, "m3", "p1")   # a member's later vote replaces the earlier one
    assert s.votes(pid) == {"p1": 3}
    s.put_vote(pid, "m2", "p2")
    assert s.votes(pid) == {"p1": 2, "p2": 1}
    s.set_pick(pid, "p1")
    assert s.get_pick(pid) == "p1"


def test_cart_idempotent_lookup(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.carts_for(pid, "o1") == []
    t = now().isoformat()
    s.record_cart(pid, "p1", "o1", "cart-a", t)
    s.record_cart(pid, "p1", "o1", "cart-a", t)    # same cart twice is one cart
    s.record_cart(pid, "p1", "o1", "cart-b", t)    # "Limit N per person" splits into more
    s.record_cart(pid, "p2", "o2", "cart-c", t)
    assert s.carts_for(pid, "o1") == ["cart-a", "cart-b"]
    assert s.carts_for(pid, "o2") == ["cart-c"]
    assert s.carts_for(pid, "o9") == []
    allc = s.carts()
    assert sorted(c["cart_id"] for c in allc) == ["cart-a", "cart-b", "cart-c"]
    assert {"plan_id", "product_id", "option_id", "cart_id", "created_at"} <= set(allc[0])


def test_expired_plan_flag(be):
    s = be.store
    pid = s.create_plan(plan())
    be.age(13)
    assert s.get_plan(pid)["city"] == "Chicago"
    be.age(2)
    got = s.get_plan(pid)
    if be.expires_to_none:      # Redis deleted the key; web layer shows the same expired page
        assert got is None
    else:
        assert got == {"expired": True}


def test_purge_deletes_children(be):
    s = be.store
    pid = s.create_plan(plan())
    s.put_card(pid, mem("m1"))
    s.save_result(pid, [Line("Scout", "scout", "x", None)], result())
    s.put_vote(pid, "m1", "p1")
    s.set_pick(pid, "p1")
    s.record_cart(pid, "p1", "o1", "cart-a", now().isoformat())
    keep = s.create_plan(plan(created_at=(now() + timedelta(days=20)).isoformat()))
    be.age(15)
    purged = s.purge_expired(now() + be.offset)
    assert purged == (0 if be.kind != "sqlite" else 1)
    assert s.get_plan(pid) is None or s.get_plan(pid) == {"expired": True}
    assert s.cards(pid) == [] and s.get_result(pid) is None and s.argue_count(pid) == 0
    assert s.votes(pid) == {} and s.get_pick(pid) is None and s.carts_for(pid, "o1") == []
    assert all(c["plan_id"] != pid for c in s.carts())
    if be.kind == "sqlite":
        assert s.get_plan(pid) is None and s.get_plan(keep)["city"] == "Chicago"


def test_rate_count_by_ip_hash(be, monkeypatch):
    monkeypatch.setenv("IP_SALT", "test-salt-not-secret")
    a, b = hash_ip("203.0.113.5"), hash_ip("203.0.113.6")
    assert a != b and len(a) == 64 and "203.0.113.5" not in a
    assert hash_ip("203.0.113.5") == a
    s = be.store
    since = now() - timedelta(days=1)
    base_a, base_all = s.count_plans(a, since), s.count_all_plans(since)   # live DB may be non-empty
    s.create_plan(plan(ip_hash=a))
    s.create_plan(plan(ip_hash=a))
    s.create_plan(plan(ip_hash=b))
    assert s.count_plans(a, since) - base_a == 2
    assert s.count_plans(b, since) >= 1
    assert s.count_all_plans(since) - base_all == 3
    assert s.count_plans(a, now() + timedelta(days=2)) == 0 or be.kind == "live"


def test_hash_ip_requires_salt(monkeypatch):
    monkeypatch.delenv("IP_SALT", raising=False)
    with pytest.raises(RuntimeError):
        hash_ip("203.0.113.5")


def test_survives_reopen(be):
    s = be.store
    pid = s.create_plan(plan())
    s.put_card(pid, mem("m1"))
    s.save_result(pid, [Line("Scout", "scout", "x", None)], result())
    s.put_vote(pid, "m1", "p1")
    s.record_cart(pid, "p1", "o1", "cart-a", now().isoformat())
    s2 = be.reopen()
    assert s2.get_plan(pid)["city"] == "Chicago"
    assert [m.id for m in s2.cards(pid)] == ["m1"]
    assert s2.get_result(pid)[1].to_dict() == result().to_dict()
    assert s2.votes(pid) == {"p1": 1} and s2.carts_for(pid, "o1") == ["cart-a"]


def test_durable_key_layout_and_ttl():
    fake = FakeUpstash()
    s = DurableStore(Kv("https://fake.upstash.io", "tok" + "_fake", transport=fake))
    pid = s.create_plan(plan(ip_hash="a" * 64))
    s.put_card(pid, mem("m1"))
    s.save_result(pid, [Line("Scout", "scout", "x", None)], result())
    s.put_vote(pid, "m1", "p1")
    s.set_pick(pid, "p1")
    s.claim_run(pid)
    s.record_cart(pid, "p1", "o1", "cart-a", now().isoformat())
    keys = fake.keys()
    assert keys and all(k.startswith("gt:") for k in keys)
    deadline = json.loads(fake.data[f"gt:plan:{pid}"])["expires_at"]
    assert 1209600 - 5 <= deadline - fake.clock <= 1209600 + 5
    for k in (f"gt:plan:{pid}", f"gt:cards:{pid}", f"gt:result:{pid}", f"gt:votes:{pid}",
              f"gt:pick:{pid}", f"gt:carts:{pid}:o1", f"gt:argues:{pid}", f"gt:cartrec:{pid}:cart-a"):
        assert k in keys, k
        assert fake.expires[k] == deadline, k
    rate = [k for k in keys if k.startswith("gt:rate:")]
    assert len(rate) == 2 and any(k.startswith("gt:rate:all:") for k in rate)
    assert all(fake.ttl(k) == 2 * DAY for k in rate)
    assert not any("203.0.113" in c for cmd in fake.calls for c in map(str, cmd))


def _durable():
    fake = FakeUpstash()
    return DurableStore(Kv("https://fake.upstash.io", "tok" + "_fake", transport=fake)), fake


def _at(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def test_child_written_late_expires_with_plan():
    s, fake = _durable()
    t0 = int(fake.clock)
    pid = s.create_plan(plan(created_at=_at(t0)))
    fake.advance(13 * DAY)
    s.put_card(pid, mem("m1"))
    s.put_vote(pid, "m1", "p1")
    s.record_cart(pid, "p1", "o1", "cart-a", now().isoformat())
    for k in (f"gt:cards:{pid}", f"gt:votes:{pid}", f"gt:carts:{pid}:o1", f"gt:cartrec:{pid}:cart-a"):
        assert fake.expires[k] == t0 + 14 * DAY, k
    fake.advance(DAY + 1)   # past t0 + 14d: plan and every child gone together
    assert s.get_plan(pid) is None and s.cards(pid) == [] and s.votes(pid) == {}
    assert s.carts_for(pid, "o1") == [] and s.carts() == []
    assert [k for k in fake.keys() if not k.startswith("gt:rate:")] == []


def test_child_write_to_missing_plan_raises():
    s, fake = _durable()
    with pytest.raises(PlanNotFound):
        s.put_card("gone0000gone0000", mem("m1"))
    with pytest.raises(PlanNotFound):
        s.record_cart("gone0000gone0000", "p1", "o1", "c", "t")
    assert fake.keys() == []


def test_sqlite_child_writes_to_missing_or_expired_plan_raise(tmp_path):
    clock = [datetime.now(timezone.utc)]
    s = SqliteStore(str(tmp_path / "s.db"), now=lambda: clock[0])
    writes = [lambda pid: s.put_card(pid, mem("m1")),
              lambda pid: s.save_result(pid, [Line("Scout", "scout", "x", None)], result()),
              lambda pid: s.put_vote(pid, "m1", "p1"),
              lambda pid: s.set_pick(pid, "p1"),
              lambda pid: s.record_cart(pid, "p1", "o1", "cart-a", now().isoformat())]
    for write in writes:                     # never existed
        with pytest.raises(PlanNotFound):
            write("gone0000gone0000")
    pid = s.create_plan(plan(created_at=clock[0].isoformat()))
    for write in writes:                     # live plan: all succeed
        write(pid)
    clock[0] += timedelta(days=15)
    for write in writes:                     # expired but not yet purged
        with pytest.raises(PlanNotFound):
            write(pid)
    # nothing leaked in under a dead plan
    assert s.cards("gone0000gone0000") == [] and s.votes("gone0000gone0000") == {}


def test_cart_records_expire_with_plan():
    s, fake = _durable()
    t0 = int(fake.clock)
    old = s.create_plan(plan(created_at=_at(t0)))
    s.record_cart(old, "p1", "o1", "cart-old", now().isoformat())
    fake.advance(10 * DAY)
    new = s.create_plan(plan(created_at=_at(t0 + 10 * DAY)))
    s.record_cart(new, "p2", "o2", "cart-new", now().isoformat())
    assert sorted(c["cart_id"] for c in s.carts()) == ["cart-new", "cart-old"]
    fake.advance(5 * DAY)   # day 15: old record gone although a newer cart was written at day 10
    assert [c["cart_id"] for c in s.carts()] == ["cart-new"]


def test_each_write_and_expiry_is_one_transaction():
    s, fake = _durable()
    pid = s.create_plan(plan())
    fake.requests.clear()
    s.put_card(pid, mem("m1"))
    txns = [b for u, b in fake.requests if u.endswith("/multi-exec")]
    assert len(txns) == 1 and [c[0] for c in txns[0]] == ["HSET", "EXPIREAT"]


# ---- cart creation ceiling, accepted price, admin throttle -------------------------------------------

def test_claim_cart_creates_is_capped_and_releasable(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.claim_cart_creates(pid, 3, 9) and s.claim_cart_creates(pid, 3, 9) and s.claim_cart_creates(pid, 3, 9)
    assert not s.claim_cart_creates(pid, 1, 9)           # at the ceiling
    assert not s.claim_cart_creates(pid, 3, 9)
    s.release_cart_creates(pid, 2)
    assert s.claim_cart_creates(pid, 2, 9) and not s.claim_cart_creates(pid, 1, 9)
    s.release_cart_creates(pid, 50)                      # never below zero
    assert s.claim_cart_creates(pid, 9, 9) and not s.claim_cart_creates(pid, 1, 9)


def test_claim_cart_creates_on_missing_plan_raises(be):
    with pytest.raises(PlanNotFound):
        be.store.claim_cart_creates("gone0000gone0000", 1, 3)


def test_accepted_price_round_trip_per_option(be):
    s = be.store
    pid = s.create_plan(plan())
    assert s.get_accepted_price(pid, "o1") is None
    s.set_accepted_price(pid, "o1", 3100)
    s.set_accepted_price(pid, "o2", 450)
    assert (s.get_accepted_price(pid, "o1"), s.get_accepted_price(pid, "o2")) == (3100, 450)
    s.set_accepted_price(pid, "o1", 3200)
    assert s.get_accepted_price(pid, "o1") == 3200
    with pytest.raises(PlanNotFound):
        s.set_accepted_price("gone0000gone0000", "o1", 1)


def test_admin_failures_count_per_ip_per_hour(be):
    s = be.store
    t = datetime(2026, 10, 21, 12, 30, tzinfo=timezone.utc)
    assert s.admin_failures("a" * 64, t) == 0
    assert [s.note_admin_failure("a" * 64, t) for _ in range(3)] == [1, 2, 3]
    assert s.admin_failures("a" * 64, t) == 3 and s.admin_failures("b" * 64, t) == 0
    assert s.admin_failures("a" * 64, t + timedelta(hours=1)) == 0       # next hour, fresh allowance


def test_cart_creation_keys_expire_with_plan_and_throttle_keys_expire():
    s, fake = _durable()
    t0 = int(fake.clock)
    pid = s.create_plan(plan(created_at=_at(t0)))
    fake.advance(5 * DAY)
    assert s.claim_cart_creates(pid, 2, 9)
    s.set_accepted_price(pid, "o1", 3100)
    for k in (f"gt:cartcreates:{pid}", f"gt:accepted:{pid}"):
        assert fake.expires[k] == t0 + 14 * DAY, k
    s.note_admin_failure("c" * 64, now())
    key = [k for k in fake.keys() if k.startswith("gt:adminfail:")][0]
    assert fake.ttl(key) == 2 * 3600
    # the claim and its deadline travel in one transaction
    fake.requests.clear()
    s.claim_cart_creates(pid, 1, 9)
    txns = [b for u, b in fake.requests if u.endswith("/multi-exec")]
    assert len(txns) == 1 and [c[0] for c in txns[0]] == ["INCRBY", "EXPIREAT"]
    fake.advance(10 * DAY)
    assert [k for k in fake.keys() if k.startswith(("gt:cartcreates", "gt:accepted"))] == []


def test_not_cartable_mark_and_expire(be):
    s, t0 = be.store, now()
    assert s.not_cartable_ids(t0) == set()
    s.mark_not_cartable("p1", t0)
    assert s.not_cartable_ids(t0) == {"p1"}
    assert s.not_cartable_ids(t0 + timedelta(days=7)) == {"p1"}
    assert s.not_cartable_ids(t0 + timedelta(days=7, seconds=1)) == set()


def test_durable_not_cartable_hash_expires_in_seven_days_refreshed_on_write():
    s, fake = _durable()
    t0 = now()
    s.mark_not_cartable("p1", t0)
    assert fake.ttl("gt:notcartable") == 7 * DAY
    fake.advance(3 * DAY)
    s.mark_not_cartable("p2", t0 + timedelta(days=3))
    assert fake.ttl("gt:notcartable") == 7 * DAY
    fake.advance(7 * DAY + 1)
    assert s.not_cartable_ids(t0 + timedelta(days=10)) == set()
