import json
import logging

import pytest

from groupthink import config
from groupthink.ls_client import CartItem, LsApiError, LsClient

FAKE_KEY = "grpn_test123"


class FakeTransport:
    """Records calls; replies from a queue of (status, headers, body) or a callable."""

    def __init__(self, replies):
        self.replies = list(replies) if not callable(replies) else replies
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if callable(self.replies):
            status, hdrs, payload = self.replies(len(self.calls), url)
        else:
            status, hdrs, payload = self.replies.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, hdrs, raw


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("LS_PARTNER_KEY", FAKE_KEY)


def _client(t):
    return LsClient(transport=t, sleep=lambda s: None)


def test_user_agent_and_bearer_set():
    t = FakeTransport([(200, {}, {"data": [], "hasMore": False, "nextCursor": None}),
                       (200, {}, {"offers": [], "next": None})])
    c = _client(t)
    c.search_partner("massage", "Chicago")
    c.search_agent("massage", "Chicago")
    partner, agent = t.calls
    assert partner["headers"]["User-Agent"] == "GroupThink/0.1 (Groupon employee contest entry)"
    assert partner["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    assert agent["headers"]["User-Agent"] == "GroupThink/0.1 (Groupon employee contest entry)"
    assert not any(k.lower() == "authorization" for k in agent["headers"])
    assert "country=US" in partner["url"] and "q=massage" in partner["url"]


def test_log_redacts_key_and_drops_auth_header(caplog):
    t = FakeTransport([(200, {}, {"data": [], "hasMore": False, "nextCursor": None})])
    with caplog.at_level(logging.DEBUG):
        _client(t).search_partner("massage", "Chicago")
    assert caplog.records
    for r in caplog.records:
        text = r.getMessage() + " " + str(r.__dict__)
        assert FAKE_KEY not in text
        assert "Authorization" not in text


def test_cursor_followed_once():
    t = FakeTransport(lambda n, url: (200, {}, {"data": [{"id": "p%d" % n}], "hasMore": True, "nextCursor": "c%d" % n}))
    hits = _client(t).search_partner("massage", "Chicago")
    assert len(t.calls) == 2
    assert "cursor=c1" in t.calls[1]["url"]
    assert [h["id"] for h in hits] == ["p1", "p2"]


def test_price_mismatch_parsed():
    body = {"code": "aborted", "message": "price moved",
            "details": {"error": "PRICE_MISMATCH", "currentPrice": 5600}}
    t = FakeTransport([(409, {}, body)])
    with pytest.raises(LsApiError) as ei:
        _client(t).create_cart([CartItem("p", "o", 2, 5200)], "ref-1")
    e = ei.value
    assert e.status == 409 and e.code == "aborted"
    assert e.error == "PRICE_MISMATCH"
    assert e.current_price_minor == 5600
    assert "Authorization" not in str(e) and FAKE_KEY not in str(e)
    sent = json.loads(t.calls[0]["body"])
    assert sent == {"country": "US", "clientReference": "ref-1",
                    "items": [{"productId": "p", "optionId": "o", "quantity": 2, "expectedPrice": 5200}]}


def test_429_retry_after():
    t = FakeTransport([(429, {"Retry-After": "0"}, {"code": "resourceExhausted"}),
                       (200, {}, {"id": "prod1"})])
    assert _client(t).get_product("prod1") == {"id": "prod1"}
    assert len(t.calls) == 2


def test_429_gives_up_after_two_retries():
    t = FakeTransport(lambda n, url: (429, {"Retry-After": "0"}, {"code": "resourceExhausted"}))
    with pytest.raises(LsApiError) as ei:
        _client(t).get_product("prod1")
    assert ei.value.status == 429
    assert len(t.calls) == 3


def test_agent_offer_cached():
    t = FakeTransport([(200, {}, {"id": "a1", "permalink": "x"})])
    c = _client(t)
    assert c.get_agent_offer("x", "Chicago")["id"] == "a1"
    assert c.get_agent_offer("x", "Chicago")["id"] == "a1"
    assert len(t.calls) == 1


def test_agent_offer_cache_expires():
    now = [1000.0]
    t = FakeTransport([(200, {}, {"id": "a1"}), (200, {}, {"id": "a1"})])
    c = LsClient(transport=t, sleep=lambda s: None, clock=lambda: now[0])
    c.get_agent_offer("x", "Chicago")
    now[0] += 3601
    c.get_agent_offer("x", "Chicago")
    assert len(t.calls) == 2


def test_search_agent_returns_summaries_as_given():
    offers = [{"id": "a1", "permalink": "p1", "title": "T", "city": "Chicago", "extra": 1}]
    t = FakeTransport([(200, {}, {"offers": offers, "next": None})])
    assert _client(t).search_agent("massage", "Chicago") == offers


def test_missing_key_message_has_no_secret(monkeypatch):
    monkeypatch.delenv("LS_PARTNER_KEY", raising=False)
    monkeypatch.setattr(config, "_keychain_read", lambda: "")
    with pytest.raises(RuntimeError) as ei:
        config.partner_key()
    assert str(ei.value) == "partner key missing"


def test_env_key_wins(monkeypatch):
    monkeypatch.setenv("LS_PARTNER_KEY", "grpn_SECRETVALUE")
    assert config.partner_key() == "grpn_SECRETVALUE"
