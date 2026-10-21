"""Carts: turn the group's winner into real attributed Partner API cart(s) and buy link(s).

Rules this module keeps:
- One cart per checkout. When the fine print limits units per person, the group's quantity is split over
  ceil(quantity / limit) carts, as evenly as possible, each at most `limit` and at least the option's
  minimum units (clientReference "<plan>-<n>"). A split that cannot meet the minimum is refused up front.
- Creation is capped per plan in the store (claim_cart_creates, ceiling 3 * checkouts_needed) BEFORE any
  create_cart call, so parallel requests on many instances cannot create unbounded real carts.
- Idempotent: carts already recorded for the plan and option are re-read with get_cart and reused when they
  are still usable (status ACTIVE, items present, expiresAt in the future). Only the missing ones are created.
  The recorded ids are treated as a set.
- The buyLink is read from get_cart right before render and used verbatim. It is never fetched here.
- A price that moved is shown, never absorbed: no link until a person confirms the new price. Once a price
  is confirmed, an existing cart at any other price is stale: not reused, a fresh cart is made at the
  confirmed price (it is not deleted, a person may already have its checkout open).
- A failed re-read of an existing cart (other than 404) stops the request instead of creating a duplicate.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .fit import Ranked
from .ls_client import CartItem, LsApiError
from .money import fmt
from .store import parse_ts

USABLE_STATUS = ("ACTIVE", "OPEN")
UNAVAILABLE_ERRORS = ("PRODUCT_NOT_CARTABLE", "INVALID_PRODUCT_ID")


@dataclass
class CartView:
    links: list[str]
    quantity: int
    total_minor: int
    checkouts_needed: int
    message: str | None
    unit_minor: int | None = None          # price per voucher the carts were made at
    precision: int = 2
    new_price_minor: int | None = None     # set when the live price differs from what the group saw
    unavailable: bool = False
    missing: int = 0                       # checkouts that have no usable cart yet
    error: str | None = None               # a plain-English problem that is neither price nor availability


class CartBusy(Exception):
    """The plan's cart-creation ceiling is reached: other requests are (or were) creating its carts."""


def checkouts_for(ranked: Ranked) -> tuple[int, list[int] | None]:
    """(checkouts_needed, per-cart quantities), or (checkouts_needed, None) when no split works.

    The limit applies per person, so each cart holds at most `limit` units; with no known limit, or one at
    or above the quantity, there is a single cart. Otherwise the quantity is spread evenly over
    ceil(qty / limit) carts so no cart falls below the option's minimum units."""
    qty = max(ranked.quantity, 1)
    limit = ranked.candidate.facts.limit_per_person
    if limit is None or limit < 1 or limit >= qty:
        return 1, [qty]
    n = math.ceil(qty / limit)
    base, extra = divmod(qty, n)
    if base < max(ranked.candidate.min_units or 1, 1):
        return n, None
    return n, [base + 1] * extra + [base] * (n - extra)


def _usable(cart: dict, now: datetime) -> bool:
    """Open, not empty, not expired. A GET after DELETE is 200 ACTIVE with no items, so status alone is not
    enough. A missing expiresAt declares no expiry; one that cannot be read is treated as expired."""
    if not isinstance(cart, dict) or str(cart.get("status") or "").upper() not in USABLE_STATUS:
        return False
    if not cart.get("items"):
        return False
    exp = cart.get("expiresAt")
    if exp is None:
        return True
    try:
        return parse_ts(exp) > now
    except (TypeError, ValueError):
        return False


def _qty(cart: dict) -> int:
    return sum(int(i.get("quantity") or 0) for i in cart.get("items") or [])


def _retail(cart: dict) -> int | None:
    """Per-voucher live price of the cart's first line, minor units, or None when the line has no price."""
    for line in cart.get("items") or []:
        price = line.get("price") if isinstance(line, dict) else None
        if isinstance(price, dict) and isinstance(price.get("retail"), (int, float)):
            return int(price["retail"])
    return None


def _line_unavailable(cart: dict) -> bool:
    return any(isinstance(i, dict) and i.get("available") is False for i in cart.get("items") or [])


def _safe_link(cart: dict) -> str | None:
    link = cart.get("buyLink")
    if not isinstance(link, str) or not link or any(ch.isspace() for ch in link):
        return None
    try:
        parts = urlsplit(link)
    except ValueError:
        return None
    return link if parts.scheme == "https" and parts.hostname else None


def _plain_message(ranked: Ranked, n: int) -> str | None:
    if n <= 1:
        return None
    limit = ranked.candidate.facts.limit_per_person
    return ("Your group needs %d vouchers and the fine print limits each checkout to %d per person, so this "
            "takes %d separate checkouts. One person pays on each." % (ranked.quantity, limit, n))


def make_carts(client, store, ranked: Ranked, plan_id: str, expected_price_minor: int | None = None,
               now: datetime | None = None, create: bool = True, confirmed: bool = False,
               solo: bool = False) -> CartView:
    """Reuse or create the carts for `ranked` and return what the page needs.

    `expected_price_minor` is the per-voucher price the person agreed to; it defaults to the price the group
    voted on. `confirmed` says a person accepted that price: existing carts at any other price are then stale
    and are replaced instead of triggering another "price changed" screen. With create=False nothing is
    created: the view lists the carts that already work. Raises CartBusy over the plan's creation ceiling.
    """
    c = ranked.candidate
    now = now or datetime.now(timezone.utc)
    expected = c.price_minor if expected_price_minor is None else int(expected_price_minor)
    n_needed, chunks = checkouts_for(ranked)

    def view(links=(), total=0, **kw):
        return CartView(links=list(links), quantity=ranked.quantity, total_minor=total,
                        checkouts_needed=n_needed, message=kw.pop("message", _plain_message(ranked, n_needed)),
                        unit_minor=kw.pop("unit_minor", expected), precision=c.precision, **kw)

    def price_changed(new):
        return view(message="Price changed to %s" % fmt(new, c.precision), new_price_minor=new, missing=n_needed)

    def gone():
        return view(message="No longer available", unavailable=True, missing=n_needed)

    if chunks is None:
        return view(message=None, error="This offer cannot be split into checkouts that meet its minimum of "
                    "%d voucher%s each. %s"
                    % (c.min_units, "" if c.min_units == 1 else "s",
                       "Go back to your picks and choose another option." if solo
                       else "Ask the group to vote for another option."))

    seen: list[str] = []
    live_carts: list[dict] = []
    for cart_id in store.carts_for(plan_id, c.option_id):
        if cart_id in seen:
            continue
        seen.append(cart_id)
        try:
            cart = client.get_cart(cart_id)
        except LsApiError as exc:
            if exc.status == 404:
                continue
            raise
        if not _usable(cart, now):
            continue
        price = _retail(cart)
        if confirmed and price is not None and price != expected:
            continue                                   # stale: made at a price nobody agreed to
        live_carts.append(cart)

    carts: list[dict] = []
    remaining = list(chunks)
    for cart in live_carts:
        if _qty(cart) in remaining:
            remaining.remove(_qty(cart))
            carts.append(cart)

    for cart in carts:
        if _line_unavailable(cart):
            return gone()
        live = _retail(cart)
        if live is not None and live != expected:
            return price_changed(live)

    if create and remaining:
        claimed = len(remaining)
        if not store.claim_cart_creates(plan_id, claimed, max(3, 3 * n_needed)):
            raise CartBusy(plan_id)
        made_count = 0
        try:
            for q in list(remaining):
                n = len(seen) + 1
                try:
                    made = client.create_cart([CartItem(c.product_id, c.option_id, q, expected)],
                                              "%s-%d" % (plan_id, n))
                except LsApiError as exc:
                    if exc.error == "PRICE_MISMATCH" and exc.current_price_minor is not None:
                        return price_changed(exc.current_price_minor)
                    if exc.error in UNAVAILABLE_ERRORS:
                        return gone()
                    raise
                made_count += 1
                cart_id = made["id"]
                store.record_cart(plan_id, c.product_id, c.option_id, cart_id, now.isoformat())
                seen.append(cart_id)
                remaining.remove(q)
                try:
                    made = client.get_cart(cart_id)        # the buy link is always the freshly re-read one
                except LsApiError:
                    pass
                if _line_unavailable(made):
                    return gone()
                carts.append(made)
        finally:
            if made_count < claimed:                       # give back what was claimed but never created
                try:
                    store.release_cart_creates(plan_id, claimed - made_count)
                except Exception:
                    pass

    links = [_safe_link(cart) for cart in carts]
    if any(link is None for link in links):
        return view(error="Groupon did not return a usable checkout link. Try again in a minute.",
                    missing=n_needed)
    totals = [(cart.get("totals") or {}).get("grandTotal") for cart in carts]
    if carts and all(isinstance(t, (int, float)) for t in totals):
        total = int(sum(totals))
    else:
        total = sum(_qty(cart) for cart in carts) * expected
    lived = _retail(carts[0]) if carts else None
    return view(links, total, unit_minor=lived if lived is not None else expected,
                missing=max(n_needed - len(carts), 0))
