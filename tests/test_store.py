import os
from datetime import datetime, timedelta, timezone

import pytest

from groupthink.candidates import Candidate
from groupthink.debate import Line
from groupthink.facts import Facts
from groupthink.fit import Member, RankResult, rank
from groupthink.kv import Kv
from groupthink.store import SqliteStore, hash_ip
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
    assert s.argue_count(pid) == 1
    s.save_result(pid, lines, res)
    assert s.argue_count(pid) == 2


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
    s.record_cart(pid, "p1", "o1", "cart-a", now().isoformat())
    keys = fake.keys()
    assert keys and all(k.startswith("gt:") for k in keys)
    for k in (f"gt:plan:{pid}", f"gt:cards:{pid}", f"gt:result:{pid}", f"gt:votes:{pid}",
              f"gt:pick:{pid}", f"gt:carts:{pid}:o1", f"gt:argues:{pid}"):
        assert k in keys, k
        assert fake.ttl(k) == 1209600, k
    rate = [k for k in keys if k.startswith("gt:rate:")]
    assert len(rate) == 2 and any(k.startswith("gt:rate:all:") for k in rate)
    assert all(fake.ttl(k) == 2 * DAY for k in rate)
    assert not any("203.0.113" in c for cmd in fake.calls for c in map(str, cmd))
