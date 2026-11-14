"""Dialogue invariants: properties that must hold in every conversation, whatever the scenario.

Scenario expectations check the business outcome. Invariants check conduct, and they catch bugs
that outcome checks miss: a call can finish with the right order while the agent still says
something wrong in the last turn.

The caller-side oracle is deliberately independent of the agent's NLU: it matches goodbye wording
directly, so a broken NLU cannot hide its own failure.
"""

from __future__ import annotations

from voiceshop.simulation.conversation import TurnRecord
from voiceshop.text import fold

GOODBYE_MARKERS = (
    "goodbye", "bye", "that's all", "that's everything", "that is all", "nothing else", "nothing more",
    "thank you, that's", "thanks, that's",
)
TASK_WORDS = ("order", "refund", "return", "cart", "buy", "purchase", "price", "stock", "recommend",
              "compare", "track", "delivery", "tent", "bag", "stove", "pack", "checkout", "check out", "change")


def is_caller_goodbye(text: str) -> bool:
    folded = fold(text)
    return any(marker in folded for marker in GOODBYE_MARKERS)


def caller_starts_task(text: str) -> bool:
    folded = fold(text)
    return any(word in folded for word in TASK_WORDS)


def dialogue_violations(turns: list[TurnRecord]) -> list[str]:
    """Return human-readable violations; an empty list means the conduct invariants held."""
    violations: list[str] = []
    for i, turn in enumerate(turns):
        if turn.speaker != "customer" or not is_caller_goodbye(turn.text_heard):
            continue
        # A goodbye that also starts a new task is not a goodbye; skip it.
        if caller_starts_task(turn.text_heard):
            continue
        following = next((t for t in turns[i + 1:] if t.speaker == "agent"), None)
        if following is None:
            continue
        if following.act != "close":
            violations.append(
                f"turn {turn.seq}: caller said goodbye ({turn.text_heard!r}) "
                f"but agent answered with '{following.act}' instead of closing the call"
            )
    return violations
