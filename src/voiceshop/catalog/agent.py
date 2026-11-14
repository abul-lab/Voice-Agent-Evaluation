"""Product assistant (English). It answers from the catalogue only.

Rules that matter for evaluation:
  * it names products, prices and stock only from the catalogue
  * it recommends only in-stock items that satisfy the shopper's stated constraints
  * when nothing matches it says so; it never invents an alternative product or a price
"""

from __future__ import annotations

import re
import time as _clock
from dataclasses import dataclass, field
from typing import Any

from voiceshop.catalog.model import Catalog, Product
from voiceshop.text import fold, has_phrase

INTENT_WORDS = {
    "stock": ("in stock", "available", "do you have", "have any", "any left", "stock"),
    "price": ("how much", "price", "cost", "costs"),
    "compare": ("compare", "versus", "vs", "difference between", "which is better", "better one"),
    "recommend": ("recommend", "suggest", "looking for", "i need", "i want", "best", "what should", "help me", "which"),
    "goodbye": ("bye", "goodbye", "thanks", "thank you", "that's all", "that is all"),
    "greeting": ("hello", "hi", "good morning", "good afternoon"),
}
SEASON_WORDS = {4: ("winter", "snow", "cold", "-18", "arctic"), 2: ("summer",), 3: ("spring", "autumn", "fall", "three season", "3 season")}
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


@dataclass
class Understanding:
    intents: list[str] = field(default_factory=list)
    category: str | None = None
    products: list[Product] = field(default_factory=list)
    people: int | None = None
    budget: int | None = None
    season: int | None = None
    lightweight: bool = False
    unknown_product_hint: bool = False

    @property
    def intent(self) -> str | None:
        return self.intents[0] if self.intents else None


@dataclass
class ProductReply:
    text: str
    act: str
    params: dict[str, Any]
    language: str
    intent: str | None
    latency_ms: float
    terminal: bool
    state: dict[str, Any]


class ProductAgent:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.language = "en"
        self.ctx: dict[str, Any] = {"category": None, "people": None, "budget": None, "season": None, "light": False}
        self.last_products: list[Product] = []
        self.ended = False

    # ----------------------------------------------------------- understanding
    def understand(self, text: str) -> Understanding:
        f = fold(text)
        u = Understanding()
        for intent, words in INTENT_WORDS.items():
            if any(has_phrase(f, w) for w in words):
                u.intents.append(intent)
        for cat, words in self.catalog.categories.items():
            if any(has_phrase(f, fold(w)) for w in words):
                u.category = cat
                break
        for p in self.catalog.products.values():
            if has_phrase(f, p.first_word) or has_phrase(f, fold(p.name)):
                u.products.append(p)
        m = re.search(r"(?:under|below|less than|max|up to|budget(?: of)?)\s*\$?\s*(\d+)", f)
        if m:
            u.budget = int(m.group(1))
        m = re.search(r"(?:for|sleeps?)\s+(\d+|one|two|three|four|five|six)\b", f) or re.search(
            r"\b(\d+|one|two|three|four|five|six)\s+(?:of us|people|persons|adults)\b", f)
        if m:
            raw = m.group(1)
            u.people = NUMBER_WORDS.get(raw) or int(raw)
        if has_phrase(f, "solo") or has_phrase(f, "just me") or has_phrase(f, "alone"):
            u.people = 1
        if has_phrase(f, "family"):
            u.people = max(u.people or 0, 4)
        for season, words in SEASON_WORDS.items():
            if any(has_phrase(f, w) for w in words):
                u.season = season
                break
        u.lightweight = any(has_phrase(f, w) for w in ("light", "lightweight", "light weight", "backpacking"))
        # A price or stock question about a named thing we do not carry.
        u.unknown_product_hint = ("price" in u.intents or "stock" in u.intents) and not u.products and bool(
            re.search(r"\b(?:the|that|this)\s+[a-z0-9-]+\s+(?:\w+\s+)?(?:tent|bag|pack|stove)\b", f))
        return u

    # ----------------------------------------------------------------- acts
    def greet(self) -> ProductReply:
        started = _clock.perf_counter()
        return self._reply(
            "greet",
            {"store": self.catalog.store_name},
            f"Hi, welcome to {self.catalog.store_name}. What can I help you find today?",
            None, started,
        )

    def respond(self, utterance: str) -> ProductReply:
        started = _clock.perf_counter()
        if self.ended:
            return self._reply("close", {}, "Thanks for visiting. Have a great trip!", None, started, terminal=True)
        u = self.understand(utterance)
        self._merge(u)

        # Pronoun follow-ups ("how much is it?") refer to the last product offered.
        if (("price" in u.intents or "stock" in u.intents) and not u.products and self.last_products
                and not u.unknown_product_hint):
            u.products = [self.last_products[0]]

        if u.intent == "goodbye" and not u.products and u.category is None:
            self.ended = True
            return self._reply("close", {}, "Thanks for visiting. Have a great trip!", u.intent, started, terminal=True)
        if u.unknown_product_hint:
            return self._reply("no_match", {}, "I couldn't find that product in our catalogue. Would you like me to suggest something similar?", u.intent, started)
        if "compare" in u.intents and len(u.products) >= 2:
            return self._compare(u.products[:2], started)
        if "price" in u.intents and u.products:
            return self._price(u.products[0], started)
        if "stock" in u.intents and u.products:
            return self._stock(u.products[0], started)
        if u.intent == "greeting" and u.category is None and not u.products:
            return self._reply("ask_intent", {}, "Hi again. What are you looking for?", u.intent, started)
        if u.category or "recommend" in u.intents:
            return self._recommend(started, u)
        if not u.intents and u.category is None and not u.products:
            return self._reply("out_of_scope", {}, "I can help with our products, prices and stock. What are you looking for?", None, started)
        return self._reply("ask_intent", {}, "Sorry, which product do you mean? Tell me the type, such as a tent, sleeping bag, backpack or stove.", u.intent, started)

    # ------------------------------------------------------------- handlers
    def _merge(self, u: Understanding) -> None:
        if u.category:
            self.ctx["category"] = u.category
        if u.people is not None:
            self.ctx["people"] = u.people
        if u.budget is not None:
            self.ctx["budget"] = u.budget
        if u.season is not None:
            self.ctx["season"] = u.season
        if u.lightweight:
            self.ctx["light"] = True

    def _recommend(self, started: float, u: Understanding) -> ProductReply:
        category = self.ctx["category"]
        if category is None:
            return self._reply("ask_intent", {}, "What type of gear are you after: tent, sleeping bag, backpack or stove?", u.intent, started)
        if category == "tent" and self.ctx["people"] is None:
            return self._reply("ask_people", {"category": category}, "How many people will be using the tent?", u.intent, started)

        candidates = self.catalog.in_category(category)
        people = self.ctx["people"] or 1
        if category == "tent":
            candidates = [p for p in candidates if p.capacity >= people]
        if self.ctx["season"]:
            candidates = [p for p in candidates if p.season >= self.ctx["season"]]
        if self.ctx["budget"] is not None:
            candidates = [p for p in candidates if p.price <= self.ctx["budget"]]
        if self.ctx["light"]:
            candidates = [p for p in candidates if self.catalog.is_lightweight(p)]
        candidates.sort(key=lambda p: p.price)

        in_stock = [p for p in candidates if p.stock > 0][: self.catalog.recommend_max]
        if in_stock:
            self.last_products = in_stock
            items = " or ".join(f"the {p.name} at ${p.price}" for p in in_stock)
            return self._reply("recommend", {"product_ids": [p.id for p in in_stock]},
                               f"For you I'd suggest {items}.", u.intent, started)
        if candidates:
            alts = candidates[: self.catalog.recommend_max]
            names = " and ".join(f"the {p.name}" for p in alts)
            return self._reply("out_of_stock", {"product_ids": [p.id for p in alts]},
                               f"We don't have anything matching that in stock right now. Our closest options are {names}, which are currently out of stock.",
                               u.intent, started)
        return self._reply("no_match", {}, "I couldn't find a product matching that. Could you tell me more about what you need?", u.intent, started)

    def _price(self, p: Product, started: float) -> ProductReply:
        self.last_products = [p]
        return self._reply("answer_price", {"product_ids": [p.id]}, f"The {p.name} costs ${p.price}.", "price", started)

    def _stock(self, p: Product, started: float) -> ProductReply:
        self.last_products = [p]
        if p.stock > 0:
            text = f"The {p.name} is in stock, with {p.stock} available."
        else:
            text = f"The {p.name} is currently out of stock."
        return self._reply("answer_stock", {"product_ids": [p.id], "in_stock": p.stock > 0}, text, "stock", started)

    def _compare(self, pair: list[Product], started: float) -> ProductReply:
        a, b = pair
        if a.category == "tent":
            detail = f"{a.name} is ${a.price} and sleeps {a.capacity}; {b.name} is ${b.price} and sleeps {b.capacity}."
        else:
            detail = f"{a.name} is ${a.price} and weighs {a.weight_kg} kg; {b.name} is ${b.price} and weighs {b.weight_kg} kg."
        return self._reply("compare", {"product_ids": [a.id, b.id]}, detail, "compare", started)

    # -------------------------------------------------------------- plumbing
    def _reply(self, act: str, params: dict[str, Any], text: str, intent: str | None, started: float,
               terminal: bool = False) -> ProductReply:
        return ProductReply(
            text=text,
            act=act,
            params=params,
            language="en",
            intent=intent,
            latency_ms=round((_clock.perf_counter() - started) * 1000, 3),
            terminal=terminal,
            state={"category": self.ctx["category"], "people": self.ctx["people"], "budget": self.ctx["budget"],
                   "season": self.ctx["season"], "light": self.ctx["light"]},
        )
