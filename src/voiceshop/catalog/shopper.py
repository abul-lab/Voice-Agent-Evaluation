"""Caller simulator for the store. A caller has one or more goals, handled in order within one call.

Goal-based means one call can cover a shopping question followed by an order check, which is how
handoffs between specialists get tested. The interface matches the earlier caller simulator, so the shared
conversation engine drives both.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from voiceshop.errors import ScenarioError
from voiceshop.simulation.conversation import CustomerUtterance

COMPLETE_ACTS = {
    "recommend", "out_of_stock", "no_match", "answer_price", "answer_stock", "compare",
    "order_status", "refund_approved", "refund_refused", "escalate",
    # storefront goals
    "added_to_cart", "cart_summary", "cart_empty", "cannot_add", "order_placed", "order_cancelled",
    "interactive_mode", "resumed", "paused", "cannot_place",
    # customer care
    "care_info", "care_handoff", "care_logged",
    # safety and guardrails: the agent's refusal is the answer
    "safety_hold", "out_of_scope", "verify_failed",
}
REPEAT_ACTS = {"greet", "ask_intent", "out_of_scope"}
GOALS = ("recommend", "price", "stock", "compare", "order", "refund",
         "add_to_cart", "cart", "checkout", "takeover", "resume", "care", "say")
WRONG_EMAIL = "not.the.owner@example.com"


@dataclass
class Goal:
    intent: str
    category_text: str | None = None
    people: int | None = None
    budget: int | None = None
    season_text: str | None = None
    lightweight: bool = False
    product_text: str | None = None
    product_text2: str | None = None
    order_no: str | None = None
    email: str | None = None
    wrong_emails: int = 0  # how many wrong email answers before the correct one
    verbose: bool = False
    decline: bool = False  # at the order checkpoint, say no
    care_text: str | None = None  # what the caller says to customer care
    say_text: str | None = None  # free text: used for guardrail, safety and noise tests


class ShopperSimulator:
    def __init__(self, goals: list[Goal], rng: random.Random, off_topic_first: bool = False) -> None:
        if not goals:
            raise ScenarioError("a caller needs at least one goal")
        self.goals = goals
        self.rng = rng
        self.off_topic_first = off_topic_first
        self.language = "en"
        self._i = 0
        self._off_topic_done = False
        self._done = False
        self._wrong_left = goals[0].wrong_emails

    @property
    def done(self) -> bool:
        return self._done

    @property
    def goal(self) -> Goal:
        return self.goals[self._i]

    # --------------------------------------------------------------- speaking
    def _say(self, text: str, label: str | None) -> CustomerUtterance:
        return CustomerUtterance(text=text, language="en", intent_label=label)

    def _goodbye(self) -> CustomerUtterance:
        self._done = True
        return CustomerUtterance(text="Thanks, that's all. Bye.", language="en", intent_label=None, is_goodbye=True)

    def _opener(self, g: Goal) -> CustomerUtterance:
        if g.intent == "price":
            return self._say(f"How much is the {g.product_text}?", "price")
        if g.intent == "stock":
            return self._say(f"Is the {g.product_text} in stock?", "stock")
        if g.intent == "compare":
            return self._say(f"Can you compare the {g.product_text} and the {g.product_text2}?", "compare")
        if g.intent == "order":
            tail = f" {g.order_no}" if g.verbose and g.order_no else ""
            return self._say(f"Where is my order{tail}?", "order")
        if g.intent == "refund":
            tail = f" for order {g.order_no}" if g.verbose and g.order_no else ""
            return self._say(f"I'd like a refund{tail}.", "refund")
        if g.intent == "add_to_cart":
            what = f"the {g.product_text}" if g.product_text else "that"
            return self._say(f"Please add {what} to my cart.", "add_to_cart")
        if g.intent == "cart":
            return self._say("What's in my cart?", "cart")
        if g.intent == "checkout":
            return self._say("I'm ready to check out.", "checkout")
        if g.intent == "takeover":
            return self._say("I'd like to browse by myself for a bit.", "takeover")
        if g.intent == "resume":
            return self._say("I'm back. What's in my cart?", "resume")
        if g.intent == "say":
            return self._say(g.say_text or "", "say")
        if g.intent == "care":
            return self._say(g.care_text or "I'd like to speak to a manager.", "care")
        parts = ["I'm looking for"]
        parts.append(f"a {g.category_text}" if g.category_text else "some gear")
        if g.season_text:
            parts.append(f"for {g.season_text}")
        if g.lightweight:
            parts.append("that's lightweight")
        if g.verbose and g.people is not None:
            parts.append("just for me" if g.people == 1 else f"for {g.people} people")
        if g.verbose and g.budget is not None:
            parts.append(f"under ${g.budget}")
        return self._say(" ".join(parts) + ".", "recommend")

    def _credentials(self, g: Goal) -> CustomerUtterance:
        email = g.email or ""
        if self._wrong_left > 0:
            self._wrong_left -= 1
            email = WRONG_EMAIL
        return self._say(f"Order {g.order_no}, email {email}.", None)

    def _advance(self) -> CustomerUtterance:
        self._i += 1
        if self._i >= len(self.goals):
            return self._goodbye()
        self._wrong_left = self.goal.wrong_emails
        nxt = self._opener(self.goal)
        # Keep the next request's own capitalisation ("I'm back" must not become "i'm back").
        return self._say("Thanks. " + nxt.text, nxt.intent_label)

    # ---------------------------------------------------------------- api
    def open(self) -> CustomerUtterance:
        if self.off_topic_first and not self._off_topic_done:
            self._off_topic_done = True
            return self._say("Do you accept my store credit card?", None)
        return self._opener(self.goal)

    def respond(self, act: str, params: dict[str, Any], agent_language: str) -> CustomerUtterance:
        g = self.goal
        # A free-text request gets one answer: whatever the agent replies, the caller then moves on.
        if g.intent == "say" and act not in ("greet", "ask_intent"):
            return self._advance()
        if act in REPEAT_ACTS:
            return self._opener(g)
        if act == "ask_people":
            if g.people is None:
                return self._goodbye()
            return self._say("Just me." if g.people == 1 else f"{g.people} of us.", None)
        if act == "ask_order":
            return self._say(f"It's {g.order_no}.", None)
        if act == "ask_email":
            email = g.email or ""
            if self._wrong_left > 0:
                self._wrong_left -= 1
                email = WRONG_EMAIL
            return self._say(f"It's {email}.", None)
        if act in ("verify_failed", "service_error"):
            return self._credentials(g)
        if act == "confirm_order":
            return self._say("No, not yet." if g.decline else "Yes, place the order.", None)
        if act == "ask_product":
            return self._say(f"The {g.product_text}.", None) if g.product_text else self._goodbye()
        if act == "escalate" or act in COMPLETE_ACTS:
            return self._advance()
        return self._goodbye()
