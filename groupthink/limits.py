"""Abuse limits for plan creation: per client IP per UTC day, and overall per UTC day.

Counts come from the store (hashed IPs only), so they survive redeploys and cold starts.
"""
from datetime import datetime, timezone

PLANS_PER_IP_PER_DAY = 20
PLANS_PER_DAY_ALL = 300


def day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def allow(store, ip_hash: str, now: datetime) -> tuple[bool, str]:
    """(True, "") when one more plan may be created, else (False, "ip" | "all")."""
    since = day_start(now)
    if store.count_plans(ip_hash, since) >= PLANS_PER_IP_PER_DAY:
        return False, "ip"
    if store.count_all_plans(since) >= PLANS_PER_DAY_ALL:
        return False, "all"
    return True, ""
