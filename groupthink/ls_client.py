"""LivingSocial Partner + Agent API client. Server-side only.

Secret handling: the Bearer key is added to request headers only; logs carry
method, path, status and ms, with any grpn_ token redacted.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass

from . import config

log = logging.getLogger("groupthink.ls")

SECRET_RE = re.compile(r"grpn_[A-Za-z0-9_]+")
PARTNER_PREFIX = "/partner_storefront"
AGENT_PREFIX = "/ls_agent_api"
CACHE_TTL_S = 3600
MAX_429_RETRIES = 2
# requests per minute (50% of the documented limits)
BUDGETS = {"partner": 300, "agent_search": 30, "agent_read": 300}


def redact(text: str) -> str:
    return SECRET_RE.sub("grpn_<redacted>", text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would forward the Authorization header to another host; surface it instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method, url, headers, body, timeout):
    """Returns (status, response_headers, raw_body). Never raises on HTTP errors; never follows redirects."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@dataclass
class CartItem:
    product_id: str
    option_id: str
    quantity: int
    expected_price_minor: int


class LsApiError(Exception):
    def __init__(self, status, code="", message="", error=None, current_price_minor=None):
        self.status = status
        self.code = code
        self.error = error
        self.current_price_minor = current_price_minor
        self.message = redact(message or "")
        super().__init__("LS API %s %s%s%s" % (status, code or "error",
                                               (" " + error) if error else "",
                                               (": " + self.message) if self.message else ""))


class _Bucket:
    """Sliding-window limiter: at most `limit` calls in any 60 s window."""

    def __init__(self, limit_per_min, clock, sleep):
        self.limit = limit_per_min
        self.clock, self.sleep = clock, sleep
        self.stamps = deque()
        self.lock = threading.Lock()

    def take(self):
        while True:
            with self.lock:
                now = self.clock()
                while self.stamps and now - self.stamps[0] >= 60.0:
                    self.stamps.popleft()
                if len(self.stamps) < self.limit:
                    self.stamps.append(now)
                    return
                wait = self.stamps[0] + 60.0 - now
            self.sleep(max(wait, 0.001))


class LsClient:
    def __init__(self, transport=urllib_transport, sleep=time.sleep, clock=time.monotonic, timeout=40):
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._timeout = timeout
        self._buckets = {k: _Bucket(v, clock, sleep) for k, v in BUDGETS.items()}
        self._offer_cache = {}
        self._cache_lock = threading.Lock()

    # ---- core -------------------------------------------------------
    def _request(self, method, path, params=None, body=None, partner=True, bucket=None):
        bucket = bucket or ("partner" if partner else "agent_read")
        url = config.BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        if partner:
            headers["Authorization"] = "Bearer " + config.partner_key()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(MAX_429_RETRIES + 1):
            self._buckets[bucket].take()
            t0 = self._clock()
            status, rheaders, raw = self._transport(method, url, headers, data, self._timeout)
            ms = int((self._clock() - t0) * 1000)
            log.info(redact("%s %s -> %s (%d ms)" % (method, path, status, ms)))
            if status == 429 and attempt < MAX_429_RETRIES:
                self._sleep(self._retry_after(rheaders, raw))
                continue
            break
        parsed = self._parse(raw)
        if not 200 <= status < 300:
            raise self._error(status, parsed)
        return parsed

    @staticmethod
    def _retry_after(rheaders, raw):
        lowered = {str(k).lower(): v for k, v in (rheaders or {}).items()}
        try:
            return min(float(lowered.get("retry-after", 2)), 30.0)
        except (TypeError, ValueError):
            return 2.0

    @staticmethod
    def _parse(raw):
        txt = redact(raw.decode("utf8", "replace")) if raw else ""
        if not txt.strip():
            return None
        try:
            return json.loads(txt)
        except ValueError:
            return txt[:500]

    @staticmethod
    def _error(status, parsed):
        if not isinstance(parsed, dict):
            return LsApiError(status, message=parsed if isinstance(parsed, str) else "")
        details = parsed.get("details") if isinstance(parsed.get("details"), dict) else {}
        cp = details.get("currentPrice")
        return LsApiError(status, code=redact(str(parsed.get("code") or "")),
                          message=str(parsed.get("message") or ""),
                          error=redact(str(details["error"])) if details.get("error") is not None else None,
                          current_price_minor=cp if isinstance(cp, int) else None)

    # ---- partner ----------------------------------------------------
    def search_partner(self, q, city, max_price_dollars=None, limit=50):
        params = {"q": q, "city": city, "country": "US", "limit": limit}
        if max_price_dollars is not None:
            params["maxPrice"] = int(max_price_dollars)
        hits = []
        for _ in range(2):  # first page plus at most one extra
            page = self._request("GET", PARTNER_PREFIX + "/products/search", params=params) or {}
            hits.extend(page.get("data") or [])
            if not page.get("hasMore") or not page.get("nextCursor"):
                break
            params = dict(params, cursor=page["nextCursor"])
        return hits

    def get_product(self, product_id):
        return self._request("GET", PARTNER_PREFIX + "/products/" + urllib.parse.quote(product_id, safe=""))

    def create_cart(self, items, client_reference):
        body = {"country": "US", "clientReference": client_reference,
                "items": [{"productId": i.product_id, "optionId": i.option_id,
                           "quantity": i.quantity, "expectedPrice": i.expected_price_minor} for i in items]}
        return self._request("POST", PARTNER_PREFIX + "/carts", body=body)

    def get_cart(self, cart_id):
        return self._request("GET", PARTNER_PREFIX + "/carts/" + urllib.parse.quote(cart_id, safe=""))

    # ---- agent (no Authorization) -----------------------------------
    def search_agent(self, q, city, limit=50):
        page = self._request("GET", AGENT_PREFIX + "/offers/search", partner=False, bucket="agent_search",
                             params={"q": q, "city": city, "country": "US", "limit": limit}) or {}
        return page.get("offers") or []

    def get_agent_offer(self, permalink, city):
        key = (permalink, city)
        now = self._clock()
        with self._cache_lock:
            hit = self._offer_cache.get(key)
            if hit and now - hit[0] < CACHE_TTL_S:
                return hit[1]
        offer = self._request("GET", AGENT_PREFIX + "/offers/" + urllib.parse.quote(permalink, safe=""),
                              partner=False, bucket="agent_read")  # city only keys the cache (spike sent no params)
        with self._cache_lock:
            self._offer_cache[key] = (now, offer)
        return offer
