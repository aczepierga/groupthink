"""DurableStore: the Store protocol on Upstash Redis via kv.Kv (Vercel has no durable disk).

One absolute deadline per plan: create_plan stores expires_at = created_at + 14 days (unix seconds) in the
plan record and EXPIREATs the plan key to it. Every child write reads that deadline and EXPIREATs the child
key to the SAME instant, so nothing outlives its plan (a card written on day 13 still dies on day 14).
A child write whose plan key is gone raises PlanNotFound (web layer shows the expired/not-found page).
Each write and its EXPIREAT travel as one /multi-exec transaction, so a key can never be left without a
deadline. purge_expired is a no-op: Redis does the deleting. Raw IPs never reach Redis.

  gt:plan:<id>                   JSON plan incl. expires_at   string
  gt:cards:<id>                  member_id -> JSON Member     hash
  gt:result:<id>                 JSON {"lines", "result"}     string
  gt:argues:<id>                 /argue runs claimed          counter (claim_run INCRs, release_run DECRs)
  gt:votes:<id>                  member_id -> product_id      hash
  gt:pick:<id>                   product_id                   string
  gt:carts:<id>:<option_id>      cart ids, append order       list
  gt:cartrec:<id>:<cart_id>      JSON cart record (carts())   string, one key per cart
  gt:rate:<ip_hash>:<yyyymmdd>, gt:rate:all:<yyyymmdd>        counters, 2-day expiry

carts() (admin export) SCANs gt:cartrec:*; records expire with their plan. Rate counts are per UTC day:
count_plans(ip, since) sums the day buckets from since's date to today.
"""
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from .debate import Line
from .fit import Member, RankResult
from .kv import Kv
from .store import RATE_TTL_SECONDS, TTL_SECONDS, PlanNotFound, new_plan_id, parse_ts

P = "gt:"


class DurableStore:
    def __init__(self, kv: Kv):
        self.kv = kv

    def _deadline(self, plan_id):
        raw = self.kv.get(P + "plan:" + plan_id)
        if not raw:
            raise PlanNotFound(plan_id)
        return int(json.loads(raw)["expires_at"])

    def _write(self, plan_id, commands, keys):
        """Run commands plus an EXPIREAT <plan deadline> per key as one transaction."""
        deadline = self._deadline(plan_id)
        return self.kv.multi_exec([list(c) for c in commands] + [["EXPIREAT", k, deadline] for k in keys])

    @staticmethod
    def _day(dt):
        return parse_ts(dt).strftime("%Y%m%d")

    # -- plans --
    def create_plan(self, plan):
        plan = dict(plan)
        plan_id = new_plan_id()
        created = parse_ts(plan.get("created_at") or datetime.now(timezone.utc))
        ip_hash = plan.pop("ip_hash", None)
        deadline = int(created.timestamp()) + TTL_SECONDS
        plan.update(id=plan_id, created_at=created.isoformat(), expires_at=deadline)
        key = P + "plan:" + plan_id
        day = self._day(created)
        rate_keys = [P + "rate:all:" + day] + ([P + "rate:" + ip_hash + ":" + day] if ip_hash else [])
        cmds = [["SET", key, json.dumps(plan)], ["EXPIREAT", key, deadline]]
        for rk in rate_keys:
            cmds += [["INCR", rk], ["EXPIRE", rk, RATE_TTL_SECONDS]]
        self.kv.multi_exec(cmds)
        return plan_id

    def get_plan(self, plan_id):
        raw = self.kv.get(P + "plan:" + plan_id)   # expired == deleted by Redis == None
        return json.loads(raw) if raw else None

    # -- cards --
    def put_card(self, plan_id, member, cap=None):
        """HSET then count: a NEW member over `cap` is taken back out (False). Racing joiners at the last
        seat can both be turned away, never both admitted."""
        key = P + "cards:" + plan_id
        out = self._write(plan_id, [["HSET", key, member.id, json.dumps(member.to_dict())]], [key])
        is_new = int(out[0] or 0) == 1
        if cap is not None and is_new and self.kv.hlen(key) > cap:
            self.kv.hdel(key, member.id)
            return False
        return True

    def cards(self, plan_id):
        return [Member.from_dict(json.loads(v)) for v in self.kv.hgetall(P + "cards:" + plan_id).values()]

    # -- debate result --
    def save_result(self, plan_id, lines, result):
        rkey = P + "result:" + plan_id
        body = json.dumps({"lines": [ln.to_dict() for ln in lines], "result": result.to_dict()})
        # a new result resets the vote: votes and pick belong to the old top options
        self._write(plan_id, [["SET", rkey, body], ["DEL", P + "votes:" + plan_id], ["DEL", P + "pick:" + plan_id]],
                    [rkey])

    def get_result(self, plan_id):
        raw = self.kv.get(P + "result:" + plan_id)
        if not raw:
            return None
        d = json.loads(raw)
        return [Line.from_dict(x) for x in d["lines"]], RankResult.from_dict(d["result"])

    def argue_count(self, plan_id):
        return int(self.kv.get(P + "argues:" + plan_id) or 0)

    def claim_run(self, plan_id, cap=2):
        """INCR the run counter (with the plan deadline, one transaction); over `cap` it is DECRed back and
        None is returned, so of N racing claims exactly the first `cap` succeed."""
        key = P + "argues:" + plan_id
        n = int(self._write(plan_id, [["INCR", key]], [key])[0])
        if n > cap:
            self.kv.decr(key)
            return None
        return n

    def release_run(self, plan_id):
        key = P + "argues:" + plan_id
        if int(self.kv.decr(key)) < 0:
            self.kv.incr(key)

    # -- votes / pick --
    def put_vote(self, plan_id, member_id, product_id):
        key = P + "votes:" + plan_id
        self._write(plan_id, [["HSET", key, member_id, product_id]], [key])

    def votes(self, plan_id):
        return dict(Counter(self.kv.hgetall(P + "votes:" + plan_id).values()))

    def set_pick(self, plan_id, product_id):
        key = P + "pick:" + plan_id
        self._write(plan_id, [["SET", key, product_id]], [key])

    def get_pick(self, plan_id):
        return self.kv.get(P + "pick:" + plan_id)

    # -- carts --
    def record_cart(self, plan_id, product_id, option_id, cart_id, created_at):
        lkey = P + "carts:%s:%s" % (plan_id, option_id)
        if cart_id in self.kv.lrange(lkey):
            return
        rec_key = P + "cartrec:%s:%s" % (plan_id, cart_id)
        rec = json.dumps({"plan_id": plan_id, "product_id": product_id, "option_id": option_id,
                          "cart_id": cart_id, "created_at": str(created_at)})
        self._write(plan_id, [["RPUSH", lkey, cart_id], ["SET", rec_key, rec]], [lkey, rec_key])

    def carts_for(self, plan_id, option_id):
        return list(self.kv.lrange(P + "carts:%s:%s" % (plan_id, option_id)))

    def carts(self):
        out = []
        for key in sorted(self.kv.scan_keys(P + "cartrec:*")):
            raw = self.kv.get(key)
            if raw:
                out.append(json.loads(raw))
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
        return 0   # every plan-scoped key is EXPIREAT the plan deadline
