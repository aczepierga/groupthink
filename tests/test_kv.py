import json

import pytest

from groupthink.kv import Kv, KvError

TOKEN = "tok" + "_SECRETVALUE123"
URL = "https://example-redis.upstash.io"


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "command": json.loads(body) if body else None})
        status, payload = self.replies.pop(0)
        return status, {}, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def _kv(replies):
    t = FakeTransport(replies)
    return Kv(URL, TOKEN, transport=t), t


def test_kv_client_bearer_and_no_token_in_errors():
    kv, t = _kv([(200, {"result": 7})])
    assert kv.incr("k") == 7
    call = t.calls[0]
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN
    assert call["method"] == "POST" and call["url"] == URL
    assert call["command"] == ["INCR", "k"]
    assert TOKEN not in call["url"]

    # server echoes the token in an error body, and a transport exception carries it too
    kv, _ = _kv([(401, {"error": "bad token " + TOKEN})])
    with pytest.raises(KvError) as e1:
        kv.get("k")
    assert TOKEN not in str(e1.value) and TOKEN not in repr(e1.value)

    kv, _ = _kv([(200, {"error": "ERR " + TOKEN})])
    with pytest.raises(KvError) as e2:
        kv.get("k")
    assert TOKEN not in str(e2.value)

    def boom(method, url, headers, body, timeout):
        raise OSError("connect failed with " + headers["Authorization"])

    with pytest.raises(KvError) as e3:
        Kv(URL, TOKEN, transport=boom).get("k")
    assert TOKEN not in str(e3.value) and TOKEN not in repr(e3.value)
    assert e3.value.__cause__ is None and e3.value.__suppress_context__


def test_commands_map_to_redis():
    kv, t = _kv([(200, {"result": "v"}), (200, {"result": "OK"}), (200, {"result": "OK"}),
                 (200, {"result": 1}), (200, {"result": 1})])
    assert kv.get("a") == "v"
    kv.set("a", "b", ex_seconds=60)
    kv.set("a", "b")
    assert kv.expire("a", 30) is True
    assert kv.delete("a") == 1
    assert [c["command"] for c in t.calls] == [
        ["GET", "a"], ["SET", "a", "b", "EX", 60], ["SET", "a", "b"], ["EXPIRE", "a", 30], ["DEL", "a"]]


def test_get_missing_key_is_none():
    kv, _ = _kv([(200, {"result": None})])
    assert kv.get("nope") is None


def test_from_env_prefers_kv_names_then_upstash(monkeypatch):
    for n in ("KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    assert Kv.from_env() is None
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://up.example")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "up-token")
    kv = Kv.from_env()
    assert kv is not None and kv.url == "https://up.example"
    monkeypatch.setenv("KV_REST_API_URL", "https://kv.example")
    monkeypatch.setenv("KV_REST_API_TOKEN", "kv-token")
    assert Kv.from_env().url == "https://kv.example"
    # url without token is not configured
    monkeypatch.delenv("KV_REST_API_TOKEN")
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN")
    assert Kv.from_env() is None


def test_list_and_hash_commands():
    kv, t = _kv([(200, {"result": 1}), (200, {"result": ["a", "b"]}), (200, {"result": None}),
                 (200, {"result": 1}), (200, {"result": ["f1", "v1", "f2", "v2"]}), (200, {"result": []})])
    assert kv.rpush("l", "a") == 1
    assert kv.lrange("l", 0, -1) == ["a", "b"]
    assert kv.lrange("l") == []          # missing list comes back null
    assert kv.hset("h", "f1", "v1") == 1
    assert kv.hgetall("h") == {"f1": "v1", "f2": "v2"}
    assert kv.hgetall("none") == {}
    assert [c["command"] for c in t.calls] == [
        ["RPUSH", "l", "a"], ["LRANGE", "l", 0, -1], ["LRANGE", "l", 0, -1],
        ["HSET", "h", "f1", "v1"], ["HGETALL", "h"], ["HGETALL", "none"]]


def test_expireat_and_scan_commands():
    kv, t = _kv([(200, {"result": 1}), (200, {"result": ["5", ["a"]]}), (200, {"result": ["0", ["b"]]})])
    assert kv.expireat("k", 1800000000) is True
    assert kv.scan_keys("gt:x:*") == ["a", "b"]
    assert [c["command"] for c in t.calls] == [
        ["EXPIREAT", "k", 1800000000], ["SCAN", "0", "MATCH", "gt:x:*", "COUNT", 200],
        ["SCAN", "5", "MATCH", "gt:x:*", "COUNT", 200]]


def test_multi_exec_path_body_and_errors():
    kv, t = _kv([(200, [{"result": 1}, {"result": 1}])])
    assert kv.multi_exec([["HSET", "h", "f", "v"], ["EXPIREAT", "h", 1800000000]]) == [1, 1]
    call = t.calls[0]
    assert call["url"] == URL + "/multi-exec" and call["method"] == "POST"
    assert call["command"] == [["HSET", "h", "f", "v"], ["EXPIREAT", "h", 1800000000]]
    assert call["headers"]["Authorization"] == "Bearer " + TOKEN

    kv, _ = _kv([(200, [{"result": 1}, {"error": "ERR bad " + TOKEN}])])   # one element failed
    with pytest.raises(KvError) as e1:
        kv.multi_exec([["A"], ["B"]])
    assert TOKEN not in str(e1.value)

    kv, _ = _kv([(200, {"error": "ERR discarded"})])                       # whole transaction discarded
    with pytest.raises(KvError):
        kv.multi_exec([["A"]])
    kv, _ = _kv([(500, b"oops")])
    with pytest.raises(KvError):
        kv.multi_exec([["A"]])


def test_repr_hides_token():
    kv = Kv(URL, TOKEN, transport=FakeTransport([]))
    assert TOKEN not in repr(kv) and TOKEN not in str(kv)
