"""Conversation metrics: task completion, corrections, interruptions, latency and escalations.

Computed from the run records the suite already produced (their ``conversation`` turns and ``checks``), so the
numbers can be recomputed from ``report.json`` without running anything again. Report only: none of these
values gates a release yet.

Definitions are operational, so each number can be checked by hand against a transcript. See docs/roadmap.md.
"""

from __future__ import annotations

import re
import statistics
from typing import Any

from voiceshop.evaluation.analysis import _p

# A caller turn that opens with one of these is treated as a correction of what the agent just did.
CORRECTION_CUE = re.compile(r"^\s*(no\b|not\b|wrong\b|actually\b|i meant\b|i said\b|sorry\b|that's not\b)", re.I)

# Agent acts that ask a yes/no question. A caller's "no" in reply is an answer, not a correction.
CONFIRMATION_ACTS = {"confirm_order", "confirm_refund"}

# Agent acts that hand the caller to a person.
ESCALATION_ACTS = {"escalate", "care_handoff"}

# Agent turns that repair a misunderstanding (roadmap 3.2). "out_of_scope" also answers off-topic requests,
# so the repair count includes those; the handoff count does not.
REPAIR_ACTS = {"out_of_scope", "clarify", "offer_options", "recovery_handoff"}


def _corrections(conversation: list[dict[str, Any]]) -> tuple[int, int]:
    """Returns (corrections, caller turns) for one conversation."""
    corrections = 0
    caller_turns = 0
    previous_act: str | None = None
    for turn in conversation:
        if turn.get("speaker") == "customer":
            caller_turns += 1
            text = turn.get("text_heard") or turn.get("text_spoken") or ""
            if CORRECTION_CUE.match(text) and previous_act not in CONFIRMATION_ACTS:
                corrections += 1
        elif turn.get("speaker") == "agent":
            previous_act = turn.get("act")
    return corrections, caller_turns


def _escalated(conversation: list[dict[str, Any]]) -> bool:
    return any(t.get("speaker") == "agent" and t.get("act") in ESCALATION_ACTS for t in conversation)


def _interruptions(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Measurable only for turns a channel recorded as interrupted (see speech/barge_in.py). Text runs have
    no overlapping speech, so they report that the number is unknown rather than zero."""
    recorded = [t for r in records for t in r.get("conversation", []) if "interrupted" in t]
    if not recorded:
        return {"measurable": False, "count": None,
                "reason": "no channel recorded overlapping speech; text runs have none (roadmap 3.3)"}
    cut = [t for t in recorded if t.get("interrupted")]
    latencies = [float(t["stop_latency_ms"]) for t in cut if t.get("stop_latency_ms") is not None]
    within = [t for t in cut if t.get("within_budget")]
    return {
        "measurable": True,
        "count": len(cut),
        "agent_turns_recorded": len(recorded),
        "interruption_rate": round(len(cut) / len(recorded), 4),
        "stop_latency_p95_ms": _p(latencies, 95),
        "within_stop_budget_rate": round(len(within) / len(cut), 4) if cut else None,
    }


def conversation_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Phase 1 metrics over every record in a suite."""
    total = len(records)
    completed = sum(1 for r in records if r.get("passed"))

    correction_count = 0
    caller_turns = 0
    for r in records:
        c, n = _corrections(r.get("conversation", []))
        correction_count += c
        caller_turns += n

    agent_latencies = [
        float(t["latency_ms"])
        for r in records
        for t in r.get("conversation", [])
        if t.get("speaker") == "agent" and t.get("latency_ms") is not None
    ]

    repair_turns = 0
    cases_with_repair = 0
    handed_over = 0
    for r in records:
        acts = [t.get("act") for t in r.get("conversation", []) if t.get("speaker") == "agent"]
        repairs = sum(1 for a in acts if a in REPAIR_ACTS)
        repair_turns += repairs
        if repairs:
            cases_with_repair += 1
        if "recovery_handoff" in acts:
            handed_over += 1

    escalated = [r for r in records if _escalated(r.get("conversation", []))]
    unexpected = [
        r for r in escalated
        if not any(c.get("name") == "escalation_code" for c in r.get("checks", []))
    ]

    return {
        "task_completion": {
            "cases": total,
            "completed": completed,
            "completion_rate": round(completed / total, 4) if total else None,
        },
        "corrections": {
            "count": correction_count,
            "caller_turns": caller_turns,
            "rate_per_caller_turn": round(correction_count / caller_turns, 4) if caller_turns else None,
        },
        "interruptions": _interruptions(records),
        "latency": {
            "turns": len(agent_latencies),
            "p50_ms": _p(agent_latencies, 50),
            "p95_ms": _p(agent_latencies, 95),
            "max_ms": round(max(agent_latencies), 2) if agent_latencies else None,
            "mean_ms": round(statistics.fmean(agent_latencies), 2) if agent_latencies else None,
        },
        "repairs": {
            "repair_turns": repair_turns,
            "cases_with_repair": cases_with_repair,
            "cases_handed_over_by_repair": handed_over,
            "turns_per_repaired_case": round(repair_turns / cases_with_repair, 2) if cases_with_repair else None,
        },
        "escalations": {
            "cases_escalated": len(escalated),
            "escalation_rate": round(len(escalated) / total, 4) if total else None,
            "unexpected": len(unexpected),
            "unexpected_cases": [r["scenario_id"] for r in unexpected],
        },
    }
