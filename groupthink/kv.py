"""Minimal Upstash Redis REST client (stdlib only).

Secret handling: the token travels only in the Authorization header. It is
never logged, never in a URL, and stripped from every error message.
"""
import json
import os

from .ls_client import urllib_transport

TIMEOUT_S = 10
URL_ENVS = ("KV_REST_API_URL", "UPSTASH_REDIS_REST_URL")
TOKEN_ENVS = ("KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_TOKEN")


class KvError(Exception):
    pass


def _first_env(names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


class Kv:
    def __init__(self, url, token, transport=urllib_transport):
        self.url = url.rstrip("/")
        self._token = token
        self._transport = transport

    def __repr__(self):
        return "Kv(url=%r)" % self.url

    @classmethod
    def from_env(cls):
        """KV_REST_API_* first, then UPSTASH_REDIS_REST_*; None when url or token is missing."""
        url, token = _first_env(URL_ENVS), _first_env(TOKEN_ENVS)
        return cls(url, token) if url and token else None

    def _scrub(self, text):
        return str(text).replace(self._token, "<redacted>")[:200]

    def _command(self, *parts):
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        try:
            status, _, raw = self._transport("POST", self.url, headers, json.dumps(list(parts)).encode(), TIMEOUT_S)
        except Exception as exc:  # message could carry request headers; keep the type only
            raise KvError("KV transport failed: " + type(exc).__name__) from None
        try:
            payload = json.loads(raw.decode("utf8", "replace")) if raw else {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if not 200 <= status < 300:
            raise KvError("KV HTTP %s: %s" % (status, self._scrub(payload.get("error", ""))))
        if payload.get("error"):
            raise KvError("KV error: " + self._scrub(payload["error"]))
        return payload.get("result")

    def get(self, key):
        return self._command("GET", key)

    def set(self, key, value, ex_seconds=None):
        if ex_seconds is None:
            return self._command("SET", key, value)
        return self._command("SET", key, value, "EX", int(ex_seconds))

    def incr(self, key):
        return self._command("INCR", key)

    def expire(self, key, seconds):
        return bool(self._command("EXPIRE", key, int(seconds)))

    def delete(self, key):
        return self._command("DEL", key)

    def rpush(self, key, value):
        return self._command("RPUSH", key, value)

    def lrange(self, key, start=0, stop=-1):
        return self._command("LRANGE", key, int(start), int(stop)) or []

    def hset(self, key, field, value):
        return self._command("HSET", key, field, value)

    def hgetall(self, key):
        """Upstash returns a flat [field, value, ...] list; fold it to a dict."""
        flat = self._command("HGETALL", key) or []
        return dict(zip(flat[0::2], flat[1::2]))
