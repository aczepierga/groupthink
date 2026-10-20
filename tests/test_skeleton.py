import re

import pytest
from fastapi.testclient import TestClient

from groupthink.kv import KvError
from groupthink.web_skeleton import create_app

REQUIRED_COPY = ("You're buying a voucher. It does not book a date or time - "
                 "most of these need an appointment with the business after purchase.")
CANARY = "grpn" + "_CANARY0000"  # built at runtime so the source tree never holds a grpn_ literal
ENV_NAMES = ("PROBES_ENABLED", "CANARY_KEY", "KV_REST_API_URL", "KV_REST_API_TOKEN",
             "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client():
    return TestClient(create_app(), base_url="https://testserver")


@pytest.fixture
def probes_on(monkeypatch):
    monkeypatch.setenv("PROBES_ENABLED", "1")


def test_home_has_required_copy(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<title>GroupThink" in r.text
    assert REQUIRED_COPY in r.text


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.parametrize("path", ["/_probe/whoami", "/_probe/egress", "/_probe/cookie",
                                  "/_probe/count", "/_probe/canary"])
def test_probes_404_when_disabled(client, monkeypatch, path):
    assert client.get(path).status_code == 404
    monkeypatch.setenv("PROBES_ENABLED", "0")
    assert client.get(path).status_code == 404
    monkeypatch.setenv("PROBES_ENABLED", "true")  # only the exact string "1" enables
    assert client.get(path).status_code == 404


def test_whoami_returns_headers_only(client, probes_on):
    r = client.get("/_probe/whoami", headers={
        "x-forwarded-for": "203.0.113.7, 10.0.0.1", "x-real-ip": "203.0.113.7",
        "x-vercel-forwarded-for": "203.0.113.7", "authorization": "Bearer should-not-echo",
        "cookie": "gt_probe=1"})
    body = r.json()
    assert set(body) == {"client_host", "x_forwarded_for", "x_real_ip", "x_vercel_forwarded_for"}
    assert body["x_forwarded_for"] == "203.0.113.7, 10.0.0.1"
    assert body["x_real_ip"] == "203.0.113.7"
    assert body["x_vercel_forwarded_for"] == "203.0.113.7"
    assert "should-not-echo" not in r.text
    missing = client.get("/_probe/whoami").json()
    assert missing["x_forwarded_for"] is None and missing["x_real_ip"] is None


def test_canary_never_returns_value(client, probes_on, monkeypatch):
    assert client.get("/_probe/canary").json() == {"set": False, "len": 0}
    monkeypatch.setenv("CANARY_KEY", CANARY)
    r = client.get("/_probe/canary")
    assert r.json() == {"set": True, "len": len(CANARY)}
    assert CANARY not in r.text
    assert "CANARY0000" not in r.text
    assert "CANARY0000" not in client.get("/").text
    assert "CANARY0000" not in client.get("/_probe/whoami").text


def test_count_without_kv_is_null(client, probes_on):
    r = client.get("/_probe/count")
    assert r.status_code == 200
    assert r.json() == {"count": None, "kv": "not configured"}


def test_count_with_kv_increments(probes_on):
    class FakeKv:
        n = 0

        def incr(self, key):
            assert key == "gt:probe:count"
            FakeKv.n += 1
            return FakeKv.n

    c = TestClient(create_app(kv_factory=FakeKv), base_url="https://testserver")
    assert c.get("/_probe/count").json() == {"count": 1}
    assert c.get("/_probe/count").json() == {"count": 2}


def test_count_kv_error_is_502_without_detail(probes_on):
    class BrokenKv:
        def incr(self, key):
            raise KvError("upstream said 500")

    c = TestClient(create_app(kv_factory=BrokenKv), base_url="https://testserver")
    r = c.get("/_probe/count")
    assert r.status_code == 502
    assert r.json() == {"count": None, "kv": "error"}


def test_cookie_probe_second_visit_has_cookie(client, probes_on):
    first = client.get("/_probe/cookie")
    assert first.json() == {"had_cookie": False}
    set_cookie = first.headers["set-cookie"].lower()
    assert "gt_probe=1" in set_cookie
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
    assert client.get("/_probe/cookie").json() == {"had_cookie": True}


def test_egress_probe_uses_user_agent_and_returns_status_only(probes_on):
    from groupthink import config
    calls = []

    def fake_transport(method, url, headers, body, timeout):
        calls.append((method, url, dict(headers)))
        return 200, {"x-secret-ish": "nope"}, b'{"detail":"body must not be echoed"}'

    c = TestClient(create_app(egress_transport=fake_transport), base_url="https://testserver")
    r = c.get("/_probe/egress")
    assert r.json() == {"status": 200}
    method, url, headers = calls[0]
    assert (method, url) == ("GET", "https://api-core.livingsocial.com/ls_agent_api/status")
    assert headers["User-Agent"] == config.USER_AGENT
    assert "Authorization" not in headers


def test_egress_probe_network_failure_is_status_zero(probes_on):
    def boom(method, url, headers, body, timeout):
        raise OSError("dns failure")

    c = TestClient(create_app(egress_transport=boom), base_url="https://testserver")
    assert c.get("/_probe/egress").json() == {"status": 0}


def test_no_inline_handlers_on_home(client):
    html = client.get("/").text
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert "javascript:" not in html.lower()
    assert '<script src="/static/reveal.js"' in html


def test_static_files_served(client):
    css = client.get("/static/style.css")
    assert css.status_code == 200
    js = client.get("/static/reveal.js")
    assert js.status_code == 200


def test_no_dashes_in_served_text(client):
    for path in ("/", "/static/style.css", "/static/reveal.js"):
        text = client.get(path).text
        assert "—" not in text and "–" not in text, path
