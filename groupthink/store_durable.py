"""DurableStore: the Store protocol on Upstash Redis via kv.Kv (Vercel has no durable disk).

Key layout, all under the gt: namespace. Every plan-scoped key carries EX 1209600 (14 days), set on every
write, so Redis expiry does the purging and purge_expired is a no-op. Raw IPs never reach Redis.

  gt:plan:<id>               JSON plan                      string
  gt:cards:<id>              member_id -> JSON Member       hash
  gt:result:<id>             JSON {"lines", "result"}       string
  gt:argues:<id>             /argue runs                    counter
  gt:votes:<id>              member_id -> product_id        hash
  gt:pick:<id>               product_id                     string
  gt:carts:<id>:<option_id>  cart ids, append order         list
  gt:cartlog                 JSON cart records, all plans   list (rolling TTL, filtered by created_at)
  gt:rate:<ip_hash>:<yyyymmdd>, gt:rate:all:<yyyymmdd>      counters, 2-day EX

Rate counts are per UTC day: count_plans(ip, since) sums the day buckets from since's date to today.
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from .debate import Line
from .fit import Member, RankResult
from .kv import Kv
from .store import RATE_TTL_SECONDS, TTL_DAYS, TTL_SECONDS, new_plan_id, parse_ts

P = "gt:"


class DurableStore:
    def __init__(self, kv: Kv):
        self.kv = kv

    # -- helpers --
    def _set(self, key, value):
        self.kv.set(key, value, ex_seconds=TTL_SECONDS)

    def _get_json(self, key):
        raw = self.kv.get(key)
        return json.loads(raw) if raw else None

    @staticmethod
    def _day(dt):
        return parse_ts(dt).strftime("%Y%m%d")

    # -- plans --
    def create_plan(self, plan):
        plan = dict(plan)
        plan_id = new_plan_id()
        created = parse_ts(plan.get("created_at") or datetime.now(timezone.utc))
        ip_hash = plan.pop("ip_hash", None)
        plan.update(id=plan_id, created_at=created.isoformat())
        self._set(P + "plan:" + plan_id, json.dumps(plan))
        day = self._day(created)
        keys = [P + "rate:all:" + day] + ([P + "rate:" + ip_hash + ":" + day] if ip_hash else [])
        for key in keys:
            self.kv.incr(key)
            self.kv.expire(key, RATE_TTL_SECONDS)
        return plan_id

    def get_plan(self, plan_id):
        return self._get_json(P + "plan:" + plan_id)   # expired == deleted by Redis == None

    # -- cards --
    def put_card(self, plan_id, member):
        key = P + "cards:" + plan_id
        self.kv.hset(key, member.id, json.dumps(member.to_dict()))
        self.kv.expire(key, TTL_SECONDS)

    def cards(self, plan_id):
        return [Member.from_dict(json.loads(v)) for v in self.kv.hgetall(P + "cards:" + plan_id).values()]

    # -- debate result --
    def save_result(self, plan_id, lines, result):
        self._set(P + "result:" + plan_id,
                  json.dumps({"lines": [ln.to_dict() for ln in lines], "result": result.to_dict()}))
        key = P + "argues:" + plan_id
        self.kv.incr(key)
        self.kv.expire(key, TTL_SECONDS)

    def get_result(self, plan_id):
        d = self._get_json(P + "result:" + plan_id)
        if d is None:
            return None
        return [Line.from_dict(x) for x in d["lines"]], RankResult.from_dict(d["result"])

    def argue_count(self, plan_id):
        return int(self.kv.get(P + "argues:" + plan_id) or 0)

    # -- votes / pick --
    def put_vote(self, plan_id, member_id, product_id):
        key = P + "votes:" + plan_id
        self.kv.hset(key, member_id, product_id)
        self.kv.expire(key, TTL_SECONDS)

    def votes(self, plan_id):
        return dict(Counter(self.kv.hgetall(P + "votes:" + plan_id).values()))

    def set_pick(self, plan_id, product_id):
        self._set(P + "pick:" + plan_id, product_id)

    def get_pick(self, plan_id):
        return self.kv.get(P + "pick:" + plan_id)

    # -- carts --
    def record_cart(self, plan_id, product_id, option_id, cart_id, created_at):
        key = P + "carts:%s:%s" % (plan_id, option_id)
        if cart_id in self.kv.lrange(key):
            return
        self.kv.rpush(key, cart_id)
        self.kv.expire(key, TTL_SECONDS)
        self.kv.rpush(P + "cartlog", json.dumps({"plan_id": plan_id, "product_id": product_id,
                                                  "option_id": option_id, "cart_id": cart_id,
                                                  "created_at": str(created_at)}))
        self.kv.expire(P + "cartlog", TTL_SECONDS)

    def carts_for(self, plan_id, option_id):
        return list(self.kv.lrange(P + "carts:%s:%s" % (plan_id, option_id)))

    def carts(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
        out = []
        for raw in self.kv.lrange(P + "cartlog"):
            rec = json.loads(raw)
            try:
                if parse_ts(rec["created_at"]) < cutoff:
                    continue
            except (ValueError, KeyError):
                pass
            out.append(rec)
        return out

    # -- rate counters --
    def _sum_days(self, prefix, since):
        first, today = parse_ts(since).date(), datetime.now(timezone.utc).date()
        total, day = 0, first
        while day <= today:
            total += int(self.kv.get(prefix + day.strftime("%Y%m%d")) or 0)
            day += timedelta(days=1)
        return total

    def count_plans(self, ip_hash, since):
        return self._sum_days(P + "rate:" + ip_hash + ":", since)

    def count_all_plans(self, since):
        return self._sum_days(P + "rate:all:", since)

    def purge_expired(self, now):
        return 0   # every plan-scoped key expires in Redis at 14 days
