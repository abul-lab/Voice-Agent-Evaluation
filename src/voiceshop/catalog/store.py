"""Store back office: order data, refund policy, and the tool gateway every agent must go through.

Enterprise controls implemented here (and tested in tests/test_store.py):
  * least privilege: each agent has an explicit scope list; calls outside it are denied and audited
  * audit trail: every tool call (allowed, denied, failed, duplicate) is written to the audit log
  * idempotent refunds: one approved refund per order, retries return the original result
  * versioned policy: refusals cite a rule ID and the policy version
  * fault handling: a timed-out back-office call surfaces as a service error, never as a guess
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from voiceshop.catalog import shop as _shop
from voiceshop.catalog.model import Catalog
from voiceshop.errors import ConfigError
from voiceshop.storage.repository import AuditLog, RefundLedger

SCOPES: dict[str, frozenset[str]] = {
    "shopping": frozenset({"catalog:read", "cart:write", "checkout:write"}),
    "order": frozenset({"orders:read"}),
    "refund": frozenset({"orders:read", "refunds:write"}),
    "care": frozenset(),  # least privilege: care answers from published information and hands off to a person
}
TOOL_SCOPE = {
    "lookup_order": "orders:read",
    "issue_refund": "refunds:write",
    "navigate": "catalog:read",
    "add_to_cart": "cart:write",
    "set_mode": "cart:write",
    "request_order": "checkout:write",
    "cancel_order": "checkout:write",
    "place_order": "checkout:write",
}


class ToolDenied(Exception):
    """Raised when an agent calls a tool outside its scopes."""


class ToolFault(Exception):
    """A back-office call timed out or failed. Callers must not guess the answer."""


@dataclass(frozen=True)
class Order:
    id: str
    email: str
    status: str
    delivered_on: date | None
    eta: date | None
    tracking: str | None
    items: tuple[str, ...]
    total: int
    final_sale: bool


@dataclass(frozen=True)
class Decision:
    approved: bool
    rule_id: str | None
    policy_version: str
    detail: str


class StoreData:
    def __init__(self, path: Path) -> None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"store data not readable: {path}") from exc
        self.policy = doc["policy"]
        self.window_days = int(self.policy["refund_window_days"])
        self.orders: dict[str, Order] = {}
        for o in doc["orders"]:
            self.orders[o["id"]] = Order(
                id=o["id"],
                email=o["email"].lower(),
                status=o["status"],
                delivered_on=date.fromisoformat(o["delivered_on"]) if o.get("delivered_on") else None,
                eta=date.fromisoformat(o["eta"]) if o.get("eta") else None,
                tracking=o.get("tracking"),
                items=tuple(o["items"]),
                total=int(o["total"]),
                final_sale=bool(o["final_sale"]),
            )

    def evaluate_refund(self, order: Order, today: date) -> Decision:
        """Rules are checked in order; the first failing rule is the one cited."""
        version = self.policy["version"]
        if order.status != "delivered" or order.delivered_on is None:
            return Decision(False, "REFUND-DELIVERED", version, "not delivered yet")
        if order.final_sale:
            return Decision(False, "REFUND-FINAL-SALE", version, "final-sale item")
        days = (today - order.delivered_on).days
        if days > self.window_days:
            return Decision(False, "REFUND-WINDOW-30", version, f"delivered {days} days ago")
        return Decision(True, None, version, f"delivered {days} days ago")

    def prices(self) -> set[int]:
        return {o.total for o in self.orders.values()}


class ToolGateway:
    """The only path from an agent to back-office data. Scope checks, faults and audit happen here."""

    def __init__(self, store: StoreData, audit: AuditLog, tenant: str, ledger: RefundLedger,
                 faults: dict[str, int] | None = None,
                 *, catalog: Catalog | None = None, shops: _shop.ShopSessions | None = None) -> None:
        self.store = store
        self.audit = audit
        self.tenant = tenant
        self.ledger = ledger
        self.faults = dict(faults or {})
        self.catalog = catalog
        self.shops = shops

    def _authorise(self, agent: str, tool: str) -> None:
        need = TOOL_SCOPE[tool]
        if need not in SCOPES[agent]:
            self.audit.record(self.tenant, agent, "tool.denied", tool, scope=need)
            raise ToolDenied(f"{agent} agent lacks scope {need}")

    def _maybe_fault(self, agent: str, tool: str) -> None:
        if self.faults.get(tool, 0) > 0:
            self.faults[tool] -= 1
            self.audit.record(self.tenant, agent, "tool.fault", tool)
            raise ToolFault(f"{tool} timed out")

    def lookup_order(self, agent: str, order_no: str) -> Order | None:
        self._authorise(agent, "lookup_order")
        self._maybe_fault(agent, "lookup_order")
        order = self.store.orders.get(order_no)
        self.audit.record(self.tenant, agent, "tool.lookup_order", order_no, found=order is not None)
        return order

    def issue_refund(self, agent: str, order: Order, amount: int, policy_version: str) -> tuple[dict[str, Any], bool]:
        """Idempotent on order id. Returns (record, duplicate)."""
        self._authorise(agent, "issue_refund")
        self._maybe_fault(agent, "issue_refund")
        record, duplicate = self.ledger.issue(self.tenant, order.id, amount, policy_version, agent)
        if duplicate:
            self.audit.record(self.tenant, agent, "refund.duplicate", order.id)
            return {"order": order.id, "amount": record["amount"], "policy_version": record["policy_version"],
                    "status": record["status"]}, True
        self.audit.record(self.tenant, agent, "refund.approved", order.id, amount=amount,
                          policy_version=policy_version)
        return {"order": order.id, "amount": amount, "policy_version": policy_version, "status": "approved"}, False

    def record_refusal(self, agent: str, order_no: str, rule_id: str, policy_version: str) -> None:
        self.audit.record(self.tenant, agent, "refund.refused", order_no, rule_id=rule_id,
                          policy_version=policy_version)

    def existing_refund(self, agent: str, order_id: str) -> dict[str, Any] | None:
        """The approval already on record for this order, if any. Read without approving anything."""
        self._authorise(agent, "issue_refund")
        return self.ledger.get(self.tenant, order_id)

    def record_decline(self, agent: str, order_id: str, amount: int) -> None:
        """The caller said no to a refund the agent had read back. Nothing is approved."""
        self.audit.record(self.tenant, agent, "refund.declined", order_id, amount=amount)

    # ------------------------------------------------------- storefront tools
    def _shop_call(self, agent: str, tool: str, sid: str, action: str, fn, **detail: Any) -> Any:
        """Every storefront change by an agent is scope-checked, applied atomically, and audited."""
        self._authorise(agent, tool)
        if self.shops is None or self.catalog is None:
            raise ConfigError("storefront tools need a catalogue and a session store")
        try:
            result = self.shops.mutate(sid, self.catalog, fn, actor=agent, action=action, detail=detail)
        except _shop.ShopError as exc:
            self.audit.record(self.tenant, agent, "shop.refused", sid, rule=exc.code, attempted=action)
            raise
        self.audit.record(self.tenant, agent, f"shop.{action}", sid, **detail)
        return result

    def add_to_cart(self, agent: str, sid: str, pid: str):
        return self._shop_call(agent, "add_to_cart", sid, "add_to_cart",
                               lambda s: _shop.op_add(s, self.catalog, pid), product=pid)

    def navigate(self, agent: str, sid: str, page: str) -> None:
        self._shop_call(agent, "navigate", sid, "navigate", lambda s: _shop.op_navigate(s, page), page=page)

    def set_mode(self, agent: str, sid: str, mode: str) -> None:
        self._shop_call(agent, "set_mode", sid, "set_mode", lambda s: _shop.op_set_mode(s, mode), mode=mode)

    def request_order(self, agent: str, sid: str) -> int:
        return self._shop_call(agent, "request_order", sid, "request_order",
                               lambda s: _shop.op_request_order(s, self.catalog))

    def cancel_order(self, agent: str, sid: str) -> None:
        self._shop_call(agent, "cancel_order", sid, "cancel_order", _shop.op_cancel_order)

    def place_order(self, agent: str, sid: str) -> dict[str, Any]:
        order = self._shop_call(agent, "place_order", sid, "place_order",
                                lambda s: _shop.op_place_order(s, self.catalog))
        self.audit.record(self.tenant, agent, "order.placed", order["id"], total=order["total"])
        return order
