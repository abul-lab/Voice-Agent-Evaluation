"""Four specialist agents behind a front desk, for Eliza Voice Shopping.

  front desk (the "conductor", a rules router, not a model): picks the specialist for each caller turn
  shopping : product questions, recommendations, cart, checkout (after confirmation)
             scope: catalog:read, cart:write, checkout:write
  order    : order status, after verifying order number + email          scope: orders:read
  refund   : refund decision against the versioned policy, after verification
             scope: orders:read, refunds:write
  care     : complaints, store information and handoff to a person          scope: none

Specialists never talk to the back office directly; they call the ToolGateway, which enforces scopes and
writes the audit trail. Verification state lives in one session shared by the order and refund agents, so
a caller who has verified does not have to do it again after a handoff. The shopping cart lives in the
storefront session, so the shopper can switch to clicking and back without losing anything.
"""

from __future__ import annotations

import re
import time as _clock
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from voiceshop.catalog.agent import ProductAgent
from voiceshop.catalog.model import Catalog
from voiceshop.catalog.shop import SAVED_ADDRESS, TEST_CARD, ShopError, ShopSessions
from voiceshop.catalog.store import Order, StoreData, ToolFault, ToolGateway
from voiceshop.text import fold, has_phrase

BRAND = "Eliza Voice Shopping"
NAMES = {"shopping": "shopping team", "order": "order tracking team", "refund": "refunds team",
         "care": "customer care team"}
ORDER_RE = re.compile(r"\bnw-(\d{4})\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # stops before a sentence-ending full stop
REFUND_CUES = ("refund", "return", "money back", "send it back", "give me my money")
CARE_COMPLAINT_CUES = ("complain", "complaint", "feedback", "unhappy", "rude", "not happy", "bad service")
CARE_HUMAN_CUES = ("manager", "supervisor", "real person", "human", "speak to someone", "talk to someone",
                   "speak to a person", "talk to a person")
CARE_INFO_CUES = ("opening hours", "store hours", "open today", "phone number", "contact details", "contact you",
                  "how do i contact", "how can i contact", "your location", "email address")
# Published store information the care agent may quote. Fictional brand, example contact details.
CARE_INFO = {"hours": "9 am to 6 pm, Monday to Saturday", "phone": "0800 555 0142", "email": "care@example.com"}
COMMERCE_CUES = (
    "add to cart", "add to my cart", "add it", "add that", "add the", "put it in", "buy it", "buy this",
    "i'll take", "i will take", "my cart", "my basket", "in my cart", "check out", "checkout", "place my order",
    "place the order", "place order", "buy now", "browse by myself", "by myself", "take over", "let me browse",
    "i'll browse", "shop myself", "by clicks", "i'm back", "resume", "talk again", "back to voice",
)
ORDER_CUES = ("my order", "order status", "track", "tracking", "where is", "delivery", "shipping", "shipped",
              "arrive", "arriving", "when will")
SHOP_CUES = ("looking for", "recommend", "suggest", "need a", "need some", "want a", "which", "best",
             "help me find", "buy", "compare", "in stock", "how much", "price", "cost")
ADD_CUES = ("add to cart", "add to my cart", "add it", "add that", "add the", "put it in", "buy it", "buy this",
            "i'll take", "i will take")
CART_CUES = ("my cart", "my basket", "in my cart")
CHECKOUT_CUES = ("check out", "checkout", "place my order", "place the order", "place order", "buy now")
TAKEOVER_CUES = ("browse by myself", "by myself", "take over", "let me browse", "i'll browse", "shop myself",
                 "by clicks")
RESUME_CUES = ("i'm back", "resume", "talk again", "back to voice")
# Safety, strict: once the caller says they are driving, the voice channel does no shopping, order or refund
# work for the rest of the call. It only listens, answers with the safety message, and ends the call if asked.
# The caller is back to normal service only after saying they have parked.
DRIVING_CUES = ("i'm driving", "i am driving", "while driving", "driving right now", "im driving", "on the road",
                "behind the wheel")
PARKED_CUES = ("i'm parked", "im parked", "i am parked", "i've parked", "i have parked", "parked now",
               "i've pulled over", "i pulled over", "i'm stopped", "i am stopped", "i've stopped")
SAFETY_TEXT = ("For your safety, I can't help with shopping, orders or refunds by voice while you're driving. "
               "Please pull over and tell me when you've parked.")
YES_WORDS = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "go", "ahead", "please"}
NO_WORDS = {"no", "not", "cancel", "wait", "later", "dont"}
SERVICE_TEXT = ("I'm having trouble reaching our order system. Please give me the details again in a moment.")


def _has(f: str, cues: tuple[str, ...]) -> bool:
    return any(has_phrase(f, c) for c in cues)


@dataclass
class Session:
    order_no: str | None = None
    email: str | None = None
    verified: str | None = None
    failed: int = 0


@dataclass
class StoreReply:
    text: str
    act: str
    params: dict[str, Any]
    language: str
    intent: str | None
    latency_ms: float
    terminal: bool
    state: dict[str, Any]
    agent: str
    stages: dict[str, float] = field(default_factory=dict)  # ms per step of this reply


class Conductor:
    """Routes each caller turn to one specialist and returns that specialist's reply."""

    def __init__(self, catalog: Catalog, store: StoreData, gateway: ToolGateway, today: date,
                 sessions: ShopSessions | None = None, sid: str | None = None) -> None:
        self.catalog = catalog
        self.store = store
        self.gateway = gateway
        self.today = today
        self.shop = ProductAgent(catalog)  # product NLU and product answers
        self.sessions = sessions
        self.sid = sid
        self.session = Session()
        self.active: str | None = None
        self.path: list[str] = []
        self.ended = False
        self._stage: dict[str, float] = {}
        self.driving = False  # set by "I'm driving", cleared by "I've parked"

    # ------------------------------------------------------------- entry
    def greet(self) -> StoreReply:
        started = _clock.perf_counter()
        text = (f"Hi, welcome to {BRAND}. I can help you find gear, add it to your cart, check out, "
                "track an order or request a refund. What would you like to do?")
        return self._reply("router", "greet", {}, text, None, started)

    def respond(self, utterance: str) -> StoreReply:
        started = _clock.perf_counter()
        self._stage = {}
        if self.ended:
            return self._reply("router", "close", {}, f"Thanks for shopping with {BRAND}. Have a great day!",
                               None, started, terminal=True)
        f = fold(utterance)
        u = self.shop.understand(utterance)
        t_understood = _clock.perf_counter()
        self._stage["understand_ms"] = round((t_understood - started) * 1000, 3)
        self._capture(utterance)
        if _has(f, DRIVING_CUES):
            self.driving = True
        elif _has(f, PARKED_CUES):
            self.driving = False
        if self.driving:
            if u.intent == "goodbye":
                self.ended = True
                return self._reply("router", "close", {}, f"Thanks for shopping with {BRAND}. Drive safely!",
                                   u.intent, started, terminal=True)
            return self._reply("router", "safety_hold", {}, SAFETY_TEXT, u.intent, started)
        target = self._route(utterance, f, u)
        t_routed = _clock.perf_counter()
        self._stage["route_ms"] = round((t_routed - t_understood) * 1000, 3)

        if target == "close":
            self.ended = True
            return self._reply("router", "close", {}, f"Thanks for shopping with {BRAND}. Have a great day!",
                               u.intent, started, terminal=True)
        if target is None:
            if u.intent == "greeting":
                return self._reply("router", "ask_intent", {}, "Hi again. What can I help you with?", u.intent, started)
            return self._reply("router", "out_of_scope", {},
                               "I can help you find gear, shop your cart, track orders and handle refunds.",
                               u.intent, started)

        switched = target != self.active
        self.active = target
        self.path.append(target)
        act, params, body = self._dispatch(target, utterance, f, u)
        self._stage["dispatch_ms"] = round((_clock.perf_counter() - t_routed) * 1000, 3)
        text = f"I'll connect you with our {NAMES[target]}. {body}" if switched else body
        return self._reply(target, act, params, text, u.intent, started)

    # ------------------------------------------------------------ routing
    def _capture(self, utterance: str) -> None:
        m = ORDER_RE.search(utterance.lower())
        if m:
            order_no = f"NW-{m.group(1)}"
            if self.session.verified and self.session.verified != order_no:
                self.session.verified = None
            self.session.order_no = order_no
        e = EMAIL_RE.search(utterance)
        if e:
            self.session.email = e.group(0).lower()

    def _route(self, utterance: str, f: str, u) -> str | None:
        if _has(f, REFUND_CUES):
            return "refund"
        if _has(f, CARE_COMPLAINT_CUES) or _has(f, CARE_HUMAN_CUES) or _has(f, CARE_INFO_CUES):
            return "care"
        # Commerce cues come before order cues: "place my order" must reach the shopping agent.
        if self.sessions is not None and _has(f, COMMERCE_CUES):
            return "shopping"
        if _has(f, ORDER_CUES):
            return "order"
        if u.products or u.category or _has(f, SHOP_CUES):
            return "shopping"
        if self.active:
            # Only details in *this* message count as an answer; earlier details do not keep the call open.
            has_details = bool(ORDER_RE.search(utterance.lower()) or EMAIL_RE.search(utterance))
            if u.intent == "goodbye" and not has_details:
                return "close"
            return self.active
        if u.intent == "goodbye":
            return "close"
        return None

    def _dispatch(self, target: str, utterance: str, f: str, u) -> tuple[str, dict[str, Any], str]:
        if target == "shopping":
            return self._shopping(utterance, f, u)
        if target == "order":
            return self._order()
        if target == "care":
            return self._care(f)
        return self._refund()

    # ------------------------------------------------------------ customer care
    def _care(self, f: str) -> tuple[str, dict[str, Any], str]:
        """No back-office tools: quotes published store information, or hands the caller to a person."""
        if _has(f, CARE_INFO_CUES):
            return ("care_info", {"topic": "store"},
                    f"We're open {CARE_INFO['hours']}. You can reach a person on {CARE_INFO['phone']} "
                    f"or at {CARE_INFO['email']}.")
        if _has(f, CARE_HUMAN_CUES):
            return ("care_handoff", {"reason_code": "CARE_HANDOFF"},
                    "I'll put you through to a member of our support team now. Reason code CARE_HANDOFF.")
        return ("care_logged", {}, "I'm sorry to hear that. I've logged your feedback for our customer care team. "
                                   "Could you tell me a little more about what happened?")

    # ------------------------------------------------------------- shopping
    def _shopping(self, utterance: str, f: str, u) -> tuple[str, dict[str, Any], str]:
        if self.sessions is None or self.sid is None:
            r = self.shop.respond(utterance)
            return r.act, r.params, r.text
        sid = self.sid
        sess = self.sessions.session(sid)
        g = self.gateway
        total = sum(self.catalog.get(pid).price * qty for pid, qty in sess.cart.items())
        items = sum(sess.cart.values())

        if sess.mode == "interactive":
            if _has(f, RESUME_CUES):
                g.set_mode("shopping", sid, "voice")
                return ("resumed", {"total": total},
                        f"Welcome back. Your cart has {items} item{'s' if items != 1 else ''} "
                        f"and the total is ${total}. What would you like to do next?")
            return ("paused", {},
                    "Voice is paused while you shop by clicks. Say \"I'm back\" or press Resume voice when you want me again.")

        if _has(f, TAKEOVER_CUES):
            g.set_mode("shopping", sid, "interactive")
            return ("interactive_mode", {},
                    "No problem. I've paused voice so you can shop by clicks. Your cart is saved.")

        if sess.pending == "place_order":
            if _is_no(f):
                g.cancel_order("shopping", sid)
                return ("order_cancelled", {},
                        "No problem, I haven't placed the order. Your cart is unchanged.")
            if _is_yes(f) or _has(f, CHECKOUT_CUES):
                try:
                    order = g.place_order("shopping", sid)
                except ShopError as exc:
                    return "cannot_place", {"reason": exc.code}, f"I couldn't place the order: {exc}"
                return ("order_placed", {"order": order["id"], "total": order["total"]},
                        f"Order {order['id']} is placed for ${order['total']}. It will be delivered to "
                        f"{SAVED_ADDRESS}. Anything else I can help with?")
            return ("confirm_order", {"total": total},
                    f"Shall I place the order for ${total}? Please say yes or no.")

        if _has(f, ADD_CUES):
            if u.products:
                pid = u.products[0].id
            elif self.shop.last_products:
                pid = self.shop.last_products[0].id
            else:
                return "ask_product", {}, "Which product would you like me to add to your cart?"
            name = self.catalog.get(pid).name
            try:
                p = g.add_to_cart("shopping", sid, pid)
            except ShopError:
                return ("cannot_add", {"product_ids": [pid]},
                        f"Sorry, the {name} is out of stock, so I can't add it to your cart.")
            total = sum(self.catalog.get(x).price * q for x, q in sess.cart.items())
            return ("added_to_cart", {"product_ids": [pid], "total": total},
                    f"Added the {p.name} at ${p.price} to your cart. Your total is ${total}. Shall I check out?")

        if _has(f, CHECKOUT_CUES):
            if not sess.cart:
                return "cart_empty", {}, "Your cart is empty, so there's nothing to check out yet."
            total = g.request_order("shopping", sid)
            return ("confirm_order", {"total": total},
                    f"Your total is ${total} for {items} item{'s' if items != 1 else ''}, delivered to "
                    f"{SAVED_ADDRESS} and paid with your {TEST_CARD}. Shall I place the order?")

        if _has(f, CART_CUES):
            g.navigate("shopping", sid, "cart")
            if not sess.cart:
                return "cart_empty", {}, "Your cart is empty at the moment."
            lines = ", ".join(f"{q} x {self.catalog.get(x).name}" for x, q in sess.cart.items())
            return "cart_summary", {"total": total}, f"Your cart has {lines}. The total is ${total}."

        r = self.shop.respond(utterance)
        ids = r.params.get("product_ids") or []
        if r.act == "recommend" and ids:
            g.navigate("shopping", sid, f"category:{self.catalog.get(ids[0]).category}")
        elif r.act in ("answer_price", "answer_stock", "compare") and ids:
            g.navigate("shopping", sid, f"product:{ids[0]}")
        return r.act, r.params, r.text

    # --------------------------------------------------------- verification
    def _verify(self) -> tuple[str, dict[str, Any], str] | None:
        """None means verified. Otherwise the specialist asks for the missing detail or reports the failure."""
        s = self.session
        if s.verified:
            return None
        if s.order_no is None:
            return "ask_order", {}, "Could you give me your order number? It starts with NW."
        if s.email is None:
            return "ask_email", {}, "And the email address on that order?"
        try:
            order = self.gateway.lookup_order(self.active or "order", s.order_no)
        except ToolFault:
            return "service_error", {}, SERVICE_TEXT
        if order is not None and order.email == s.email:
            s.verified = order.id
            return None
        s.failed += 1
        s.order_no = None
        s.email = None
        if s.failed >= 2:
            return ("escalate", {"code": "VERIFY_FAILED"},
                    "I can't verify those details, so I'm transferring you to our support team. "
                    "Reason code VERIFY_FAILED.")
        return ("verify_failed", {},
                "Those details don't match our records. Could you check the order number and email?")

    def _load_verified(self, agent: str) -> tuple[Order | None, tuple[str, dict[str, Any], str] | None]:
        try:
            order = self.gateway.lookup_order(agent, self.session.verified)
        except ToolFault:
            return None, ("service_error", {}, SERVICE_TEXT)
        return order, None

    # ------------------------------------------------------------ specialists
    def _order(self) -> tuple[str, dict[str, Any], str]:
        blocked = self._verify()
        if blocked:
            return blocked
        order, err = self._load_verified("order")
        if err:
            return err
        if order.status == "delivered":
            text = f"Order {order.id} was delivered on {_day(order.delivered_on)}."
        elif order.status == "shipped":
            text = (f"Order {order.id} has shipped. Tracking number {order.tracking}, "
                    f"due on {_day(order.eta)}.")
        else:
            text = f"Order {order.id} is being prepared and is due on {_day(order.eta)}."
        return "order_status", {"order": order.id, "status": order.status}, text

    def _refund(self) -> tuple[str, dict[str, Any], str]:
        blocked = self._verify()
        if blocked:
            return blocked
        order, err = self._load_verified("refund")
        if err:
            return err
        version = self.store.policy["version"]
        decision = self.store.evaluate_refund(order, self.today)
        if not decision.approved:
            self.gateway.record_refusal("refund", order.id, decision.rule_id, version)
            reasons = {
                "REFUND-DELIVERED": f"Order {order.id} hasn't been delivered yet, so it can't be refunded yet.",
                "REFUND-FINAL-SALE": f"Order {order.id} contains a final-sale item, which can't be refunded.",
                "REFUND-WINDOW-30": (f"Order {order.id} was delivered more than "
                                     f"{self.store.window_days} days ago, outside our refund window."),
            }
            text = f"{reasons[decision.rule_id]} (rule {decision.rule_id}, policy {version})"
            return "refund_refused", {"order": order.id, "rule_id": decision.rule_id,
                                      "policy_version": version}, text
        try:
            record, duplicate = self.gateway.issue_refund("refund", order, order.total, version)
        except ToolFault:
            return "service_error", {}, SERVICE_TEXT
        if duplicate:
            text = (f"A refund for order {order.id} is already approved, so no second refund was issued. "
                    f"Amount ${record['amount']}.")
        else:
            text = (f"Your refund of ${record['amount']} for order {order.id} is approved. It will go back to your "
                    f"original payment method within {self.store.policy['refund_timeline']}.")
        return "refund_approved", {"order": order.id, "amount": record["amount"], "duplicate": duplicate,
                                   "policy_version": version}, text

    # ----------------------------------------------------------------- plumbing
    def _reply(self, agent: str, act: str, params: dict[str, Any], text: str, intent: str | None,
               started: float, terminal: bool = False) -> StoreReply:
        return StoreReply(
            text=text, act=act, params=params, language="en", intent=intent,
            latency_ms=round((_clock.perf_counter() - started) * 1000, 3), terminal=terminal,
            state={"active": self.active, "verified": self.session.verified, "failed": self.session.failed},
            agent=agent, stages=dict(self._stage),
        )


def _is_yes(f: str) -> bool:
    tokens = set(f.split())
    return bool(tokens & YES_WORDS) and not _is_no(f)


def _is_no(f: str) -> bool:
    return bool(set(f.split()) & NO_WORDS)


def _day(d: date | None) -> str:
    return f"{d:%B} {d.day}" if d else "a date to be confirmed"
