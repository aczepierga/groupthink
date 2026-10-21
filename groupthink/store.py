"""Plan store: Protocol + SqliteStore (local dev and tests). DurableStore (Upstash) is in store_durable.

Plan state lives 14 days. IPs are never stored raw: callers pass hash_ip(ip) as plan["ip_hash"].
"""
import hashlib
import json
import os
import secrets
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .debate import Line
from .fit import Member, RankResult

TTL_DAYS = 14
TTL_SECONDS = TTL_DAYS * 86400
RATE_TTL_SECONDS = 2 * 86400


def hash_ip(ip: str, salt: str | None = None) -> str:
    """SHA-256 of salt + ip. Salt is env IP_SALT; refusing to hash without one beats a guessable digest."""
    salt = salt if salt is not None else os.environ.get("IP_SALT", "")
    if not salt:
        raise RuntimeError("IP_SALT is not set")
    return hashlib.sha256((salt + "|" + ip).encode("utf8")).hexdigest()


def new_plan_id() -> str:
    return secrets.token_urlsafe(12)   # 12 bytes -> 16 url-safe chars


def parse_ts(value) -> datetime:
    """ISO string or datetime -> aware UTC datetime (naive input is taken as UTC)."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class PlanNotFound(Exception):
    """Both stores refuse a child write (card, vote, result, pick, cart) when the plan is gone.

    The plan is expired or never existed; the web layer maps this to the same "expired or not found" page.
    """


class Store(Protocol):
    def create_plan(self, plan: dict) -> str: ...
    def get_plan(self, plan_id: str) -> dict | None: ...
    def put_card(self, plan_id: str, member: Member) -> None: ...
    def cards(self, plan_id: str) -> list[Member]: ...
    def save_result(self, plan_id: str, lines: list[Line], result: RankResult) -> None: ...
    def get_result(self, plan_id: str) -> tuple[list[Line], RankResult] | None: ...
    def argue_count(self, plan_id: str) -> int: ...
    def put_vote(self, plan_id: str, member_id: str, product_id: str) -> None: ...
    def votes(self, plan_id: str) -> dict[str, int]: ...
    def set_pick(self, plan_id: str, product_id: str) -> None: ...
    def get_pick(self, plan_id: str) -> str | None: ...
    def record_cart(self, plan_id: str, product_id: str, option_id: str, cart_id: str,
                    created_at: str) -> None: ...
    def carts_for(self, plan_id: str, option_id: str) -> list[str]: ...
    def carts(self) -> list[dict]: ...
    def count_plans(self, ip_hash: str, since: datetime) -> int: ...
    def count_all_plans(self, since: datetime) -> int: ...
    def purge_expired(self, now: datetime) -> int: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, ip_hash TEXT, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards (plan_id TEXT, member_id TEXT, body TEXT NOT NULL, PRIMARY KEY (plan_id, member_id));
CREATE TABLE IF NOT EXISTS results (plan_id TEXT PRIMARY KEY, body TEXT NOT NULL, argues INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS votes (plan_id TEXT, member_id TEXT, product_id TEXT NOT NULL, PRIMARY KEY (plan_id, member_id));
CREATE TABLE IF NOT EXISTS picks (plan_id TEXT PRIMARY KEY, product_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS carts (seq INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, product_id TEXT,
  option_id TEXT NOT NULL, cart_id TEXT NOT NULL, created_at TEXT, UNIQUE (plan_id, option_id, cart_id));
"""
_CHILDREN = ("cards", "results", "votes", "picks", "carts")


class SqliteStore:
    def __init__(self, path: str = ":memory:", now: Callable[[], datetime] | None = None):
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def create_plan(self, plan: dict) -> str:
        plan = dict(plan)
        plan_id = new_plan_id()
        created = parse_ts(plan.get("created_at") or self._now()).isoformat()
        ip_hash = plan.pop("ip_hash", None)
        plan.update(id=plan_id, created_at=created)
        self._db.execute("INSERT INTO plans VALUES (?,?,?,?)", (plan_id, created, ip_hash, json.dumps(plan)))
        self._db.commit()
        return plan_id

    def _require_plan(self, plan_id):
        """Child writes need a live plan (same contract as DurableStore)."""
        row = self._db.execute("SELECT created_at FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None or self._now() - parse_ts(row[0]) > timedelta(days=TTL_DAYS):
            raise PlanNotFound(plan_id)

    def get_plan(self, plan_id):
        row = self._db.execute("SELECT created_at, body FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        if self._now() - parse_ts(row[0]) > timedelta(days=TTL_DAYS):
            return {"expired": True}
        return json.loads(row[1])

    def put_card(self, plan_id, member):
        self._require_plan(plan_id)
        self._db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?)",
                         (plan_id, member.id, json.dumps(member.to_dict())))
        self._db.commit()

    def cards(self, plan_id):
        rows = self._db.execute("SELECT body FROM cards WHERE plan_id=? ORDER BY rowid", (plan_id,))
        return [Member.from_dict(json.loads(r[0])) for r in rows]

    def save_result(self, plan_id, lines, result):
        self._require_plan(plan_id)
        body = json.dumps({"lines": [ln.to_dict() for ln in lines], "result": result.to_dict()})
        self._db.execute(
            "INSERT INTO results VALUES (?,?,1) ON CONFLICT(plan_id) DO UPDATE SET body=excluded.body, argues=argues+1",
            (plan_id, body))
        self._db.commit()

    def get_result(self, plan_id):
        row = self._db.execute("SELECT body FROM results WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        d = json.loads(row[0])
        return [Line.from_dict(x) for x in d["lines"]], RankResult.from_dict(d["result"])

    def argue_count(self, plan_id):
        row = self._db.execute("SELECT argues FROM results WHERE plan_id=?", (plan_id,)).fetchone()
        return row[0] if row else 0

    def put_vote(self, plan_id, member_id, product_id):
        self._require_plan(plan_id)
        self._db.execute("INSERT OR REPLACE INTO votes VALUES (?,?,?)", (plan_id, member_id, product_id))
        self._db.commit()

    def votes(self, plan_id):
        rows = self._db.execute("SELECT product_id FROM votes WHERE plan_id=?", (plan_id,))
        return dict(Counter(r[0] for r in rows))

    def set_pick(self, plan_id, product_id):
        self._require_plan(plan_id)
        self._db.execute("INSERT OR REPLACE INTO picks VALUES (?,?)", (plan_id, product_id))
        self._db.commit()

    def get_pick(self, plan_id):
        row = self._db.execute("SELECT product_id FROM picks WHERE plan_id=?", (plan_id,)).fetchone()
        return row[0] if row else None

    def record_cart(self, plan_id, product_id, option_id, cart_id, created_at):
        self._require_plan(plan_id)
        self._db.execute("INSERT OR IGNORE INTO carts (plan_id, product_id, option_id, cart_id, created_at) "
                         "VALUES (?,?,?,?,?)", (plan_id, product_id, option_id, cart_id, str(created_at)))
        self._db.commit()

    def carts_for(self, plan_id, option_id):
        rows = self._db.execute("SELECT cart_id FROM carts WHERE plan_id=? AND option_id=? ORDER BY seq",
                                (plan_id, option_id))
        return [r[0] for r in rows]

    def carts(self):
        rows = self._db.execute("SELECT plan_id, product_id, option_id, cart_id, created_at FROM carts ORDER BY seq")
        return [dict(zip(("plan_id", "product_id", "option_id", "cart_id", "created_at"), r)) for r in rows]

    def count_plans(self, ip_hash, since):
        row = self._db.execute("SELECT COUNT(*) FROM plans WHERE ip_hash=? AND created_at>=?",
                               (ip_hash, parse_ts(since).isoformat())).fetchone()
        return row[0]

    def count_all_plans(self, since):
        row = self._db.execute("SELECT COUNT(*) FROM plans WHERE created_at>=?",
                               (parse_ts(since).isoformat(),)).fetchone()
        return row[0]

    def purge_expired(self, now):
        cutoff = (parse_ts(now) - timedelta(days=TTL_DAYS)).isoformat()
        ids = [r[0] for r in self._db.execute("SELECT id FROM plans WHERE created_at<?", (cutoff,))]
        for pid in ids:
            for table in _CHILDREN:
                self._db.execute("DELETE FROM %s WHERE plan_id=?" % table, (pid,))
            self._db.execute("DELETE FROM plans WHERE id=?", (pid,))
        self._db.commit()
        return len(ids)
