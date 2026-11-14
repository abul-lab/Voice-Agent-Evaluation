"""Gemini as the front desk's router: it names the team that should handle a shopper's message.

Only the choice of team is delegated. Verification, refund policy, the safety hold and the tool gateway stay
exactly as they are, so a wrong choice by the model cannot bypass a rule. A failed call is an error for the case,
never a silent fall-back to the rules, so the monitor shows exactly which mode was tested.
"""

from __future__ import annotations

import json
from typing import Any

from voiceshop.net import ServiceError, post_json

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TEAMS = ("shopping", "order", "refund", "care")
PROMPT = """You route one message from a shopper to a shop assistant team.

Teams:
- shopping: finding products, prices, stock, the cart, or checking out
- order: the status of an existing order
- refund: returning an item or asking for a refund
- care: a complaint, store hours or contact details, or asking for a person
- none: anything else, such as a greeting or a goodbye

Shopper said: "{utterance}"

Answer only with JSON in this form: {{"team": "shopping" | "order" | "refund" | "care" | "none"}}"""


class GeminiRouteClassifier:
    def __init__(self, api_key: str, model: str = "gemini-3.5-flash", timeout_s: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.calls = 0

    def classify(self, utterance: str) -> str | None:
        """The team for this message, or None when Gemini says 'none'. Raises ServiceError on any failure."""
        self.calls += 1
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": PROMPT.format(utterance=utterance)}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }
        data = post_json(ENDPOINT.format(model=self.model), body, {"x-goog-api-key": self.api_key},
                         timeout_s=self.timeout_s, attempts=3)
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            team = json.loads(text).get("team")
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ServiceError("the router returned no usable team") from None
        if team == "none":
            return None
        if team not in TEAMS:
            raise ServiceError(f"the router named an unknown team: {team!r}")
        return team
