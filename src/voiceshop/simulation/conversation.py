"""Turn-taking engine: runs one conversation between the voice agent and the customer.

Both speakers' turns are recorded as ``TurnRecord`` objects and emitted through ``on_turn`` as they
happen, which is how the live monitor and the database stay in step with the simulation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from voiceshop.simulation.asr import AsrNoise


@dataclass
class TurnRecord:
    """Column-compatible with the ``turns`` table. For customer turns ``intent`` is the ground-truth
    label; for agent turns it is the intent the agent detected in the preceding customer utterance."""

    seq: int
    speaker: str
    language: str
    text_spoken: str
    text_heard: str
    act: str | None
    intent: str | None
    latency_ms: float | None
    created_at: str
    agent: str | None = None  # which specialist spoke (multi-agent domains); None for single-agent runs
    shop_version: int | None = None  # storefront version when this turn was spoken (keeps the page in step)
    stages: dict | None = None  # agent turns: milliseconds per step (understand, route, dispatch)


@dataclass
class ConversationResult:
    turns: list[TurnRecord]
    end_reason: str  # terminal_act | customer_done | max_turns
    agent_errors: int


OnTurn = Callable[[TurnRecord], None]


@dataclass
class CustomerUtterance:
    """One line said by the simulated caller. ``intent_label`` is the ground-truth goal of the line."""

    text: str
    language: str
    intent_label: str | None
    is_goodbye: bool = False
    is_abandon: bool = False


class AgentLike(Protocol):
    """What the conversation loop needs from an agent: a greeting, and a reply to each heard line."""

    def greet(self) -> Any: ...

    def respond(self, utterance: str) -> Any: ...


class CallerLike(Protocol):
    """What the conversation loop needs from a caller: an opening line, then a line after each reply."""

    def open(self) -> CustomerUtterance: ...

    def respond(self, act: str, params: dict[str, Any], language: str) -> CustomerUtterance: ...


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def run_conversation(
    agent: AgentLike,
    customer: CallerLike,
    asr: AsrNoise,
    *,
    max_turns: int,
    on_turn: OnTurn | None = None,
) -> ConversationResult:
    turns: list[TurnRecord] = []
    errors = 0

    def emit(record: TurnRecord) -> None:
        turns.append(record)
        if on_turn:
            on_turn(record)

    def record_agent(reply: Any) -> None:
        nonlocal errors
        if reply.act == "error":
            errors += 1
        emit(TurnRecord(
            seq=len(turns) + 1,
            speaker="agent",
            language=reply.language,
            text_spoken=reply.text,
            text_heard=reply.text,
            act=reply.act,
            intent=reply.intent,
            latency_ms=reply.latency_ms,
            created_at=_now(),
            agent=getattr(reply, "agent", None),
            stages=getattr(reply, "stages", None),
        ))

    greeting = agent.greet()
    record_agent(greeting)

    utterance = customer.open()
    while True:
        heard = asr.transform(utterance.text, utterance.language)
        emit(TurnRecord(
            seq=len(turns) + 1,
            speaker="customer",
            language=utterance.language,
            text_spoken=utterance.text,
            text_heard=heard,
            act=None,
            intent=utterance.intent_label,
            latency_ms=None,
            created_at=_now(),
        ))
        reply = agent.respond(heard)
        record_agent(reply)
        if reply.terminal:
            return ConversationResult(turns, "terminal_act", errors)
        if utterance.is_goodbye or utterance.is_abandon:
            # The customer ended the call; the agent has answered, so the call is over.
            return ConversationResult(turns, "customer_done", errors)
        if len(turns) >= max_turns:
            return ConversationResult(turns, "max_turns", errors)
        utterance = customer.respond(reply.act, reply.params, reply.language)
