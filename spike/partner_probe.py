"""Day-1 spike probes for GroupThink (throwaway; output is a decision, see docs/spike-report.md).

Usage: partner_probe.py <step> where step is one of:
  me         Step 2  GET /partner_storefront/me (status + scope only)
  pick       Step 3a choose 10 offers from runs/offers_raw.jsonl (no network)
  products   Step 3b GET /partner_storefront/products/{id} for the picked ids, compare options and prices
  agentlat   Step 3c time GET /ls_agent_api/offers/{permalink} x10 (no key sent)
  cart       Step 4  cart lifecycle, price mismatch, maxPrice units

Secret handling: the Partner key is read from macOS Keychain inside this process only. It is never
printed or written. Everything written to disk passes through redact(); the Authorization header
is never logged. Contact fields of /me and every buyLink/buyUrl value are dropped before writing.
buyLink/buyUrl are never fetched.
"""
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
P = HERE.parent.parent  # project root (app/spike -> app -> project)
RUNS = P / "runs"
BODIES = RUNS / "probe_bodies"
OFFERS = RUNS / "offers_raw.jsonl"
BASE = "https://api-core.livingsocial.com"
UA = "GroupThink/0.1 (Groupon employee contest entry)"
KEYCHAIN_SERVICE = "groupthink-ls-partner"
MIN_GAP = 0.3  # seconds between calls: ~200/min, under the 300/min budget
SECRET_RE = re.compile(r"grpn_[A-Za-z0-9_]+")
_state = {"last": 0.0, "key": None}


def redact(text):
    return SECRET_RE.sub("grpn_<redacted>", text)


def _key():
    if _state["key"] is None:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True)
        k = r.stdout.strip()
        if not k:
            raise SystemExit("partner key missing from keychain (service %s)" % KEYCHAIN_SERVICE)
        _state["key"] = k
    return _state["key"]


def strip_links(o):
    """Drop buyLink / buyUrl values so no buy link is stored or ever opened."""
    if isinstance(o, dict):
        return {k: ("<dropped>" if k in ("buyLink", "buyUrl") else strip_links(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [strip_links(v) for v in o]
    return o


def save(tag, obj):
    BODIES.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H%M%SZ", time.gmtime())
    path = BODIES / ("spike_%s_%s.json" % (stamp, tag))
    path.write_text(redact(json.dumps(strip_links(obj), indent=1)))
    return path


def call(method, path, params=None, body=None, partner=True, retries=2):
    """Returns (status, parsed_body_or_text, latency_ms, response_headers)."""
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if partner:
        headers["Authorization"] = "Bearer " + _key()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        gap = time.time() - _state["last"]
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        t = time.time()
        rh = {}
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                status, raw, rh = r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, raw, rh = e.code, e.read(), dict(e.headers)
        except Exception as e:  # message never contains the key (it is only in the request header)
            status, raw = 0, redact(type(e).__name__ + ": " + str(e)).encode()
        lat = int((time.time() - t) * 1000)
        _state["last"] = time.time()
        if status == 429 and attempt < retries:
            time.sleep(min(float(rh.get("Retry-After", "2") or 2), 30))
            continue
        break
    txt = raw.decode("utf8", "replace")
    try:
        parsed = json.loads(txt) if txt.strip() else None
    except Exception:
        parsed = redact(txt[:500])
    return status, parsed, lat, rh


def load_offers():
    return [json.loads(l)["offer"] for l in open(OFFERS)]


def city_of(o):
    locs = o.get("locations") or []
    return (locs[0].get("city") or "").title() if locs else ""


def state_of(o):
    locs = o.get("locations") or []
    return locs[0].get("state") if locs else None


# ---------------------------------------------------------------- Step 2
def step_me():
    st, body, lat, _ = call("GET", "/partner_storefront/me")
    sf = (body or {}).get("storefront", {}) if isinstance(body, dict) else {}
    keep = {k: sf.get(k) for k in ("status", "inventoryCountries", "inventoryStates",
                                   "category0", "category1", "category2", "maxPageSize")}
    out = {"http_status": st, "latency_ms": lat, "storefront": keep}
    print(json.dumps(out, indent=1))
    save("me", out)
    states = keep.get("inventoryStates") or []
    print("PASS" if st == 200 and keep.get("status") == "active"
          and all(s in states for s in ("IL", "TX", "NY")) else "CHECK")


# ---------------------------------------------------------------- Step 3
def step_pick():
    offers = load_offers()
    seen, pool = set(), []
    for o in offers:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        if o.get("status") != "active" or not o.get("options") or not o.get("permalink"):
            continue
        st = state_of(o)
        if st not in ("IL", "TX", "NY"):
            continue
        pool.append(o)
    # greedy spread: prefer a new (state, broad class) combination each pick, 4 IL / 3 NY / 3 TX
    def klass(o):
        t = " ".join(o.get("categories") or []).lower() + " " + (o.get("title") or "").lower()
        for k, kws in (("spa", ("massage", "spa", "facial", "reflex")), ("food", ("restaurant", "cafe", "bakery", "food", "dining", "bar")),
                       ("fitness", ("yoga", "pilates", "fitness", "gym", "dance", "class")),
                       ("fun", ("escape", "axe", "golf", "bowl", "museum", "tour", "cruise", "show", "comedy", "kart", "paint"))):
            if any(w in t for w in kws):
                return k
        return "other"
    quota = {"IL": 4, "NY": 3, "TX": 3}
    chosen, used = [], set()
    for rnd in range(3):
        for o in pool:
            st = state_of(o)
            if quota.get(st, 0) <= 0 or o in chosen:
                continue
            key = (st, klass(o))
            if rnd == 0 and key in used:
                continue
            if rnd == 1 and (klass(o),) in used and key in used:
                continue
            chosen.append(o)
            used.add(key)
            used.add((klass(o),))
            quota[st] -= 1
    chosen = chosen[:10]
    buckets = {"IL": [], "NY": [], "TX": []}
    for o in pool:
        buckets[state_of(o)].append(o)
    sel = []
    for o in chosen:
        sel.append({
            "id": o["id"], "permalink": o["permalink"], "city": city_of(o), "state": state_of(o),
            "categories": o.get("categories"), "klass": klass(o),
            "options": [{"id": op["id"], "amountMinor": (op.get("price") or {}).get("amountMinor"),
                         "minUnits": op.get("minUnits"), "maxUnits": op.get("maxUnits")}
                        for op in o["options"]],
        })
    (BODIES / "spike_ids.json").write_text(json.dumps(sel, indent=1))
    print("pool", len(pool), "bucket sizes", {k: len(v) for k, v in buckets.items()})
    for s in sel:
        print(s["id"], s["city"], s["state"], s["klass"], s["categories"][:2], len(s["options"]), "options")


def step_products():
    sel = json.loads((BODIES / "spike_ids.json").read_text())
    rows, raw = [], []
    for s in sel:
        st, body, lat, _ = call("GET", "/partner_storefront/products/" + s["id"])
        raw.append({"id": s["id"], "http_status": st, "body": body})
        row = {"id": s["id"], "city": s["city"], "http": st, "found": st == 200}
        if st == 200 and isinstance(body, dict):
            agent_opts = {o["id"]: o["amountMinor"] for o in s["options"]}
            part_opts = {o["id"]: (o.get("price") or {}).get("retail") for o in body.get("options", [])}
            row["agent_option_ids"] = len(agent_opts)
            row["partner_option_ids"] = len(part_opts)
            row["ids_match_exactly"] = set(agent_opts) == set(part_opts)
            common = set(agent_opts) & set(part_opts)
            row["common_options"] = len(common)
            row["prices_equal_all_common"] = bool(common) and all(agent_opts[i] == part_opts[i] for i in common)
            row["prices_equal_count"] = "%d/%d" % (sum(agent_opts[i] == part_opts[i] for i in common), len(common))
            row["status"] = body.get("status")
            row["availabilityRequired"] = body.get("availabilityRequired")
        else:
            row["error_code"] = (body or {}).get("code") if isinstance(body, dict) else None
            row["error_detail"] = (body or {}).get("message") if isinstance(body, dict) else body
        rows.append(row)
        print(json.dumps(row))
    save("products", raw)
    found = sum(r["found"] for r in rows)
    ids_ok = sum(1 for r in rows if r.get("ids_match_exactly"))
    px_ok = sum(1 for r in rows if r.get("prices_equal_all_common"))
    both = sum(1 for r in rows if r.get("ids_match_exactly") and r.get("prices_equal_all_common"))
    print("found %d/10, option ids match exactly %d/10, prices equal %d/10, both %d/10" % (found, ids_ok, px_ok, both))
    (BODIES / "spike_products_summary.json").write_text(json.dumps(rows, indent=1))


def step_agentlat():
    sel = json.loads((BODIES / "spike_ids.json").read_text())
    lats, rows = [], []
    for s in sel:
        st, body, lat, _ = call("GET", "/ls_agent_api/offers/" + urllib.parse.quote(s["permalink"]), partner=False)
        lats.append(lat)
        rows.append({"permalink": s["permalink"][:40], "http": st, "ms": lat})
        print(rows[-1])
    lats_sorted = sorted(lats)
    p50 = statistics.median(lats)
    # nearest-rank p95 over n=10 is the max-ish element
    p95 = lats_sorted[min(len(lats_sorted) - 1, int(round(0.95 * len(lats_sorted) + 0.5)) - 1)]
    print("n=%d p50=%.0f ms p95(nearest-rank)=%d ms max=%d ms min=%d ms" % (len(lats), p50, p95, max(lats), min(lats)))
    save("agentlat", {"rows": rows, "p50": p50, "p95": p95})


# ---------------------------------------------------------------- Step 4
def step_cart():
    # ids never match (Step 3), so the cart uses the Partner product found by title+city in titlematch
    tm = json.loads(sorted(BODIES.glob("spike_*_titlematch.json"))[-1].read_text())
    cands = [r for r in tm if r.get("found_by_title") and r.get("availabilityRequired") is False and r.get("status") == "active"]
    byid = {r["partner_id"]: r for r in cands}
    target = cands[0]["partner_id"] if cands else None
    if not target:
        print("no cartable in-scope product found")
        return
    st, prod, _, _ = call("GET", "/partner_storefront/products/" + target)
    opt = None
    for o in prod["options"]:
        if o.get("active") is not False and o.get("price") and o["minUnits"] <= 2 and (o["maxUnits"] is None or o["maxUnits"] >= 3):
            opt = o
            break
    if not opt:
        print("no option allows quantity 2 and 3 on", target)
        return
    retail = opt["price"]["retail"]
    log = {"product": target, "option": opt["id"], "retail": retail, "city": byid[target]["city"]}
    created = []
    try:
        st, c, lat, _ = call("POST", "/partner_storefront/carts", body={
            "country": "US", "clientReference": "gt-spike-" + uuid.uuid4().hex[:8],
            "items": [{"productId": target, "optionId": opt["id"], "quantity": 2, "expectedPrice": retail}]})
        log["create"] = {"http": st, "latency_ms": lat}
        if st != 200:
            log["create"]["body"] = c
            raise RuntimeError("create failed %s" % st)
        cid = c["id"]
        created.append(cid)
        log["create"].update(status=c["status"], buyLink_present=bool(c.get("buyLink")),
                             items=[{"quantity": i["quantity"], "available": i["available"], "price": i["price"]} for i in c["items"]],
                             totals=c.get("totals"), messages=c.get("messages"))
        st, g, lat, _ = call("GET", "/partner_storefront/carts/" + cid)
        log["get1"] = {"http": st, "quantities": [i["quantity"] for i in g["items"]], "buyLink_present": bool(g.get("buyLink"))}
        item_id = g["items"][0]["id"]
        log["item_id_equals_option_id"] = item_id == opt["id"]
        st, u, lat, _ = call("PATCH", "/partner_storefront/carts/%s/items/%s" % (cid, item_id),
                             body={"quantity": 3, "expectedPrice": retail})
        log["patch"] = {"http": st, "body_if_error": None if st == 200 else u,
                        "quantities": [i["quantity"] for i in u["items"]] if st == 200 else None,
                        "buyLink_present": bool(u.get("buyLink")) if st == 200 else None}
        st, g2, lat, _ = call("GET", "/partner_storefront/carts/" + cid)
        log["get2"] = {"http": st, "quantities": [i["quantity"] for i in g2["items"]]}
        # price mismatch: retail + 100
        st, m, lat, _ = call("POST", "/partner_storefront/carts", body={
            "country": "US", "clientReference": "gt-spike-mm-" + uuid.uuid4().hex[:8],
            "items": [{"productId": target, "optionId": opt["id"], "quantity": 2, "expectedPrice": retail + 100}]})
        log["price_mismatch"] = {"http": st, "body": m}
        if st == 200 and isinstance(m, dict) and m.get("id"):
            created.append(m["id"])
    finally:
        log["deletes"] = []
        for cid in created:
            st, b, lat, _ = call("DELETE", "/partner_storefront/carts/" + cid)
            log["deletes"].append({"http": st, "body": b})
        if created:
            st, b, lat, _ = call("GET", "/partner_storefront/carts/" + created[0])
            log["get_after_delete"] = {"http": st, "status": (b or {}).get("status") if isinstance(b, dict) else b}
    print(json.dumps(strip_links(log), indent=1))
    save("cart", log)

    # maxPrice units: whole dollars?
    st, s, lat, _ = call("GET", "/partner_storefront/products/search", params={
        "q": "massage", "city": "Chicago", "state": "Illinois", "country": "US", "maxPrice": 20, "limit": 50})
    res = {"http": st, "latency_ms": lat}
    if st == 200:
        hits = s["data"]
        per = []
        for h in hits:
            rs = [o["price"]["retail"] for o in h["options"] if o.get("price")]
            per.append(min(rs) if rs else None)
        res.update(n_hits=len(hits), total=s.get("total"), min_retail_per_hit=sorted(x for x in per if x is not None),
                   hits_with_any_option_over_2000=sum(1 for h in hits if any(o.get("price") and o["price"]["retail"] > 2000 for o in h["options"])),
                   hits_with_cheapest_over_2000=sum(1 for x in per if x is not None and x > 2000))
    else:
        res["body"] = s
    print(json.dumps(res, indent=1))
    save("maxprice", res)


def step_diag():
    """Step 3 diagnostic: do Partner product/option ids ever coincide with agent offer/option ids?"""
    offers = load_offers()
    agent_ids = {o["id"] for o in offers}
    agent_opt_ids = {op["id"] for o in offers for op in o.get("options", [])}
    p_ids, p_opt_ids, per = set(), set(), []
    for city, state in (("Chicago", "Illinois"), ("New York", "New York"), ("Houston", "Texas"), ("Denver", "Colorado")):
        for q in ("massage", "restaurant", "spa", "fun"):
            st, s, lat, _ = call("GET", "/partner_storefront/products/search", params={"q": q, "city": city, "state": state, "country": "US", "limit": 50})
            n = len(s["data"]) if st == 200 else None
            per.append({"city": city, "q": q, "http": st, "n": n, "total": s.get("total") if st == 200 else None})
            if st == 200:
                for h in s["data"]:
                    p_ids.add(h["id"])
                    for o in h["options"]:
                        p_opt_ids.add(o["id"])
    out = {"searches": per, "partner_products": len(p_ids), "partner_options": len(p_opt_ids),
           "agent_offers": len(agent_ids), "agent_options": len(agent_opt_ids),
           "product_id_overlap_with_agent_offer_id": len(p_ids & agent_ids),
           "option_id_overlap_with_agent_option_id": len(p_opt_ids & agent_opt_ids),
           "product_id_overlap_with_agent_option_id": len(p_ids & agent_opt_ids)}
    print(json.dumps(out, indent=1))
    save("diag", out)


STATE_NAMES = {"IL": "Illinois", "NY": "New York", "TX": "Texas"}


def _norm(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def step_titlematch():
    """Step 3 supplement: ids never match, so measure whether (title, city) finds the same deal in Partner
    and whether option (title, price) agree. Informational only; the A/B threshold is judged on ids."""
    sel = json.loads((BODIES / "spike_ids.json").read_text())
    byid = {o["id"]: o for o in load_offers()}
    rows = []
    for s in sel:
        o = byid[s["id"]]
        q = (o.get("title") or "")[:120]
        st, r, lat, _ = call("GET", "/partner_storefront/products/search", params={
            "q": q, "city": s["city"], "state": STATE_NAMES[s["state"]], "country": "US", "limit": 25})
        row = {"agent_id": s["id"], "city": s["city"], "search_http": st}
        hit = None
        if st == 200:
            row["n_hits"] = len(r["data"])
            for rank, h in enumerate(r["data"]):
                if _norm(h["title"]) == _norm(o.get("title")) or _norm(h["title"]) == _norm(o.get("experienceTitle")):
                    hit, row["rank"] = h, rank
                    break
        row["found_by_title"] = hit is not None
        if hit:
            a = {(_norm(op["title"])): op["price"]["amountMinor"] for op in o["options"] if op.get("price")}
            b = {(_norm(op["title"])): op["price"]["retail"] for op in hit["options"] if op.get("price")}
            common = set(a) & set(b)
            row.update(partner_id=hit["id"], agent_options=len(a), partner_options=len(b),
                       option_titles_in_common=len(common), prices_equal=sum(a[k] == b[k] for k in common),
                       status=hit["status"], availabilityRequired=hit["availabilityRequired"])
        rows.append(row)
        print(json.dumps(row))
    print("found by title %d/10" % sum(r["found_by_title"] for r in rows))
    save("titlematch", rows)


def _cartable():
    tm = json.loads(sorted(BODIES.glob("spike_*_titlematch.json"))[-1].read_text())
    cands = [r for r in tm if r.get("found_by_title") and r.get("availabilityRequired") is False and r.get("status") == "active"]
    st, prod, _, _ = call("GET", "/partner_storefront/products/" + cands[0]["partner_id"])
    opt = next(o for o in prod["options"] if o.get("active") is not False and o.get("price") and o["minUnits"] <= 2)
    return cands[0]["partner_id"], opt


def step_abandon():
    """What does GET return for a cart after DELETE (spec says DELETE -> 204)?"""
    pid, opt = _cartable()
    st, c, _, _ = call("POST", "/partner_storefront/carts", body={"country": "US", "clientReference": "gt-spike-ab",
                       "items": [{"productId": pid, "optionId": opt["id"], "quantity": 1, "expectedPrice": opt["price"]["retail"]}]})
    cid = c["id"]
    out = {"create": st, "create_itemCount": c["itemCount"]}
    st, b, _, rh = call("DELETE", "/partner_storefront/carts/" + cid)
    out["delete"] = {"http": st, "body": b}
    st, g, _, _ = call("GET", "/partner_storefront/carts/" + cid)
    out["get_after_delete"] = {"http": st, "status": g.get("status") if isinstance(g, dict) else g,
                               "itemCount": g.get("itemCount") if isinstance(g, dict) else None,
                               "items": len(g.get("items", [])) if isinstance(g, dict) else None}
    st, b2, _, _ = call("DELETE", "/partner_storefront/carts/" + cid)
    out["second_delete"] = {"http": st, "body": b2}
    print(json.dumps(out, indent=1))
    save("abandon", out)


def step_maxprice():
    out = []
    for q, city, state in (("restaurant", "Chicago", "Illinois"), ("massage", "New York", "New York"), ("", "Houston", "Texas")):
        for mp in (20, 2000):
            params = {"city": city, "state": state, "country": "US", "maxPrice": mp, "limit": 50}
            if q:
                params["q"] = q
            st, s, lat, _ = call("GET", "/partner_storefront/products/search", params=params)
            row = {"q": q, "city": city, "maxPrice": mp, "http": st}
            if st == 200:
                cheap = []
                for h in s["data"]:
                    rs = [o["price"]["retail"] for o in h["options"] if o.get("price")]
                    cheap.append((min(rs), max(rs)) if rs else None)
                cheap = [c for c in cheap if c]
                row.update(n=len(s["data"]), total=s["total"],
                           max_of_cheapest=max((c[0] for c in cheap), default=None),
                           hits_cheapest_over_2000=sum(c[0] > 2000 for c in cheap),
                           hits_priciest_option_over_2000=sum(c[1] > 2000 for c in cheap),
                           max_of_priciest=max((c[1] for c in cheap), default=None))
            else:
                row["body"] = s
            out.append(row)
            print(json.dumps(row))
    save("maxprice2", out)


STEPS = {"abandon": step_abandon, "maxprice": step_maxprice, "titlematch": step_titlematch, "diag": step_diag, "me": step_me, "pick": step_pick, "products": step_products, "agentlat": step_agentlat, "cart": step_cart}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in STEPS:
        raise SystemExit(__doc__)
    STEPS[sys.argv[1]]()
