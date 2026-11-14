"""Eliza Voice Shopping: the storefront session that the voice agent and the shopper's browser share.

One session per shopper (or per simulated run). The voice agent and the shopper change the same cart, the
same page and the same mode, so switching between speaking and clicking never loses anything.

Rules enforced here, not in the UI:
  * an order is placed only after the shopper has confirmed it (pending -> placed)
  * stock is checked before an item is added and before an order is placed
  * placing an order empties the cart and records the order once
  * every change bumps a version and appends to the activity log, which the storefront streams
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from voiceshop.catalog.model import Catalog, Product
from voiceshop.errors import VoiceShopError

SAVED_ADDRESS = "12 Harbour Street, Louisville, KY 40202"
TEST_CARD = "test card ending 4242"
_ORDER_SEQ = itertools.count(1001)


class ShopError(VoiceShopError):
    code = "shop_rule"


def _snap(s: ShopSession) -> dict[str, Any]:
    return {"version": s.version, "page": s.page, "mode": s.mode, "pending": s.pending,
            "cart": dict(s.cart), "orders": [dict(o) for o in s.orders]}


@dataclass
class ShopSession:
    sid: str
    cart: dict[str, int] = field(default_factory=dict)
    page: str = "home"
    mode: str = "voice"          # voice | interactive (the shopper is clicking)
    pending: str | None = None   # place_order: waiting for the shopper's confirmation
    orders: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    version: int = 0
    created: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)


class ShopSessions:
    """Storefront sessions. With a database path they survive restarts: every change is saved, sessions
    untouched for ``ttl_s`` seconds are removed, and only ``max_cached`` sessions are kept in memory.
    Without a path they live in memory only (used by scenario runs, which are sandboxes)."""

    MAX_EVENTS = 80
    MAX_HISTORY = 400
    DEFAULT_TTL_S = 24 * 3600

    def __init__(self, db_path: Path | None = None, *, ttl_s: float = DEFAULT_TTL_S, max_cached: int = 500) -> None:
        self._cond = threading.Condition()
        self._db = db_path
        self._ttl = ttl_s
        self._max_cached = max_cached
        self._sessions: OrderedDict[str, ShopSession] = OrderedDict()
        self._writes_since_evict = 0

    def session(self, sid: str) -> ShopSession:
        with self._cond:
            return self._get(sid)

    def _get(self, sid: str) -> ShopSession:
        if sid in self._sessions:
            self._sessions.move_to_end(sid)
            return self._sessions[sid]
        loaded = self._load(sid)
        if loaded is None:
            loaded = ShopSession(sid=sid)
            loaded.history.append(_snap(loaded))
            self._save(loaded)
        self._sessions[sid] = loaded
        while len(self._sessions) > self._max_cached:
            self._sessions.popitem(last=False)  # least recently used; the database still has it
        return loaded

    # ------------------------------------------------------------ persistence
    def _load(self, sid: str) -> ShopSession | None:
        if self._db is None:
            return None
        from voiceshop.storage.db import session as db_session

        with db_session(self._db) as conn:
            row = conn.execute("SELECT state FROM shop_sessions WHERE sid=?", (sid,)).fetchone()
        if row is None:
            return None
        data = json.loads(row["state"])
        return ShopSession(**data)

    def _save(self, s: ShopSession) -> None:
        if self._db is None:
            return
        from voiceshop.storage.db import session as db_session

        state = json.dumps(asdict(s), default=str)
        with db_session(self._db) as conn:
            conn.execute(
                "INSERT INTO shop_sessions (sid, state, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(sid) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (s.sid, state, time.time()),
            )

    def evict_expired(self, now: float | None = None) -> int:
        """Remove sessions untouched for longer than the time-to-live. Returns how many were removed."""
        if self._db is None:
            return 0
        from voiceshop.storage.db import session as db_session

        cutoff = (now if now is not None else time.time()) - self._ttl
        with self._cond:
            with db_session(self._db) as conn:
                removed = conn.execute("DELETE FROM shop_sessions WHERE updated_at < ?", (cutoff,)).rowcount
            for sid in [k for k, v in self._sessions.items() if v.created < cutoff]:
                self._sessions.pop(sid, None)
        return removed

    def mutate(self, sid: str, catalog: Catalog, fn: Callable[[ShopSession], Any], *, actor: str,
               action: str, detail: dict[str, Any] | None = None) -> Any:
        """Apply one change atomically. Rule violations raise before anything is recorded."""
        with self._cond:
            s = self._get(sid)
            result = fn(s)
            s.version += 1
            s.history.append(_snap(s))
            del s.history[:-self.MAX_HISTORY]
            s.events.append({"n": s.version, "ts": time.strftime("%H:%M:%S"), "actor": actor,
                             "action": action, "detail": detail or {}})
            del s.events[:-self.MAX_EVENTS]
            self._save(s)
            self._writes_since_evict += 1
            if self._writes_since_evict >= 200:  # housekeeping every so often, not on every change
                self._writes_since_evict = 0
                self.evict_expired()
            self._cond.notify_all()
            return result

    def wait_for_change(self, sid: str, version: int, timeout: float) -> ShopSession:
        with self._cond:
            s = self._get(sid)
            if s.version <= version:
                self._cond.wait(timeout)
            return s

    def snapshot(self, sid: str, catalog: Catalog) -> dict[str, Any]:
        with self._cond:
            s = self._get(sid)
            return self._render(sid, catalog, _snap(s), list(s.events[-25:]))

    def snapshot_at(self, sid: str, catalog: Catalog, version: int) -> dict[str, Any]:
        """The storefront as it was at a given version, so the page can follow the conversation turn by turn."""
        with self._cond:
            s = self._get(sid)
            best = s.history[0]
            for snap in s.history:
                if snap["version"] <= version:
                    best = snap
                else:
                    break
            events = [e for e in s.events if e["n"] <= best["version"]][-25:]
            return self._render(sid, catalog, best, events)

    @staticmethod
    def _render(sid: str, catalog: Catalog, snap: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
        lines = []
        for pid, qty in snap["cart"].items():
            p = catalog.get(pid)
            lines.append({"id": pid, "name": p.name, "price": p.price, "qty": qty, "line_total": p.price * qty})
        return {
            "sid": sid,
            "version": snap["version"],
            "page": snap["page"],
            "mode": snap["mode"],
            "pending": snap["pending"],
            "cart": lines,
            "cart_count": sum(snap["cart"].values()),
            "total": sum(line["line_total"] for line in lines),
            "orders": snap["orders"],
            "events": events,
            "address": SAVED_ADDRESS,
            "payment": TEST_CARD,
        }


# ---------------------------------------------------------------- operations
def _product(catalog: Catalog, pid: str) -> Product:
    if pid not in catalog.products:
        raise ShopError(f"unknown product {pid}", code="unknown_product")
    return catalog.products[pid]


def op_add(s: ShopSession, catalog: Catalog, pid: str) -> Product:
    p = _product(catalog, pid)
    if p.stock <= 0:
        raise ShopError(f"{p.name} is out of stock", code="out_of_stock")
    s.cart[pid] = s.cart.get(pid, 0) + 1
    s.page = f"product:{pid}"
    return p


def op_remove(s: ShopSession, pid: str) -> None:
    s.cart.pop(pid, None)


def op_navigate(s: ShopSession, page: str) -> None:
    s.page = page


def op_set_mode(s: ShopSession, mode: str) -> None:
    if mode not in ("voice", "interactive"):
        raise ShopError("mode must be voice or interactive", code="bad_mode")
    s.mode = mode


def cart_total(s: ShopSession, catalog: Catalog) -> int:
    return sum(catalog.get(pid).price * qty for pid, qty in s.cart.items())


def op_request_order(s: ShopSession, catalog: Catalog) -> int:
    if not s.cart:
        raise ShopError("the cart is empty", code="cart_empty")
    s.page = "checkout"
    s.pending = "place_order"
    return cart_total(s, catalog)


def op_cancel_order(s: ShopSession) -> None:
    s.pending = None


def op_place_order(s: ShopSession, catalog: Catalog) -> dict[str, Any]:
    if s.pending != "place_order":
        raise ShopError("the shopper has not confirmed the order", code="not_confirmed")
    if not s.cart:
        raise ShopError("the cart is empty", code="cart_empty")
    for pid in s.cart:
        if catalog.get(pid).stock <= 0:
            raise ShopError(f"{catalog.get(pid).name} is out of stock", code="out_of_stock")
    order = {
        "id": f"EVS-{next(_ORDER_SEQ)}",
        "items": [{"id": pid, "name": catalog.get(pid).name, "qty": qty, "price": catalog.get(pid).price}
                  for pid, qty in s.cart.items()],
        "total": cart_total(s, catalog),
        "status": "placed",
        "placed_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    s.orders.append(order)
    s.cart = {}
    s.pending = None
    s.page = f"confirmation:{order['id']}"
    return order
