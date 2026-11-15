"""Scores barge-in results from the browser lab (tools/barge_in/run.mjs). Proof-of-concept scale.

For each minimum-speech setting it reports:
- interruptions: how many were stopped, and the misses (the agent kept talking).
- backchannels: how many were stopped in error (the agent cut itself off for a sound it should have ignored).
- decision latency: time from the caller's true onset to the agent's audio being cut, on the audio clock.

Rates come with a 95% Wilson interval, so a small sample shows as a wide interval. A case that is not valid (the
caller spoke before the agent started) is excluded and counted.

The verdict is a feasibility check, not a release gate: a setting "works" when every valid interruption was stopped,
no backchannel was stopped, and the 95th-percentile decision latency is within budget. It is not a statistical
claim about callers in general.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

Z95 = 1.959963984540054
STOP_BUDGET_MS = 300  # from the caller's onset to the agent going quiet (see docs/barge-in-measurement.md)


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion. Behaves sensibly at 0 and n."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass
class Setting:
    min_speech_ms: int
    interruptions: int = 0  # valid cases where the caller really interrupted
    stopped: int = 0  # ... and the agent stopped
    backchannels: int = 0  # valid cases where the caller only backchanneled
    false_stops: int = 0  # ... and the agent stopped anyway
    invalid: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    false_stop_cases: list[str] = field(default_factory=list)

    @property
    def misses_count(self) -> int:
        return self.interruptions - self.stopped

    def summary(self) -> dict:
        miss_low, miss_high = wilson(self.misses_count, self.interruptions)
        false_low, false_high = wilson(self.false_stops, self.backchannels)
        p50 = percentile(self.latencies_ms, 0.5)
        p95 = percentile(self.latencies_ms, 0.95)
        reasons: list[str] = []
        if self.misses_count:
            reasons.append(f"{self.misses_count} real interruption(s) were not stopped")
        if self.false_stops:
            reasons.append(f"{self.false_stops} backchannel(s) stopped the agent")
        if p95 is not None and p95 > STOP_BUDGET_MS:
            reasons.append(f"95th percentile decision latency {p95:.0f} ms is above the {STOP_BUDGET_MS} ms budget")
        return {
            "min_speech_ms": self.min_speech_ms,
            "valid_interruptions": self.interruptions,
            "stopped": self.stopped,
            "misses": self.misses_count,
            "miss_rate_95": [round(miss_low, 4), round(miss_high, 4)],
            "valid_backchannels": self.backchannels,
            "false_stops": self.false_stops,
            "false_stop_rate_95": [round(false_low, 4), round(false_high, 4)],
            "decision_latency_p50_ms": None if p50 is None else round(p50),
            "decision_latency_p95_ms": None if p95 is None else round(p95),
            "invalid_cases": self.invalid,
            "miss_cases": self.misses,
            "false_stop_cases": self.false_stop_cases,
            "verdict": "works" if not reasons else "does not work",
            "reasons": reasons,
        }


def score(results: list[dict], cases: dict[str, dict]) -> list[dict]:
    """Group results by minimum-speech setting and score each. `cases` maps case_id to its expectation."""
    settings: dict[int, Setting] = {}
    for r in results:
        case = cases.get(r["caseId"])
        if case is None:
            raise ValueError(f"result {r['caseId']!r} has no case in cases.json")
        s = settings.setdefault(case["min_speech_ms"], Setting(case["min_speech_ms"]))
        if r.get("error") or not r.get("valid"):
            s.invalid += 1
            continue
        if case["expect"] == "stop":
            s.interruptions += 1
            if r["interrupted"]:
                s.stopped += 1
                if r.get("decisionLatencyMs") is not None:
                    s.latencies_ms.append(float(r["decisionLatencyMs"]))
            else:
                s.misses.append(r["caseId"])
        else:
            s.backchannels += 1
            if r["interrupted"]:
                s.false_stops += 1
                s.false_stop_cases.append(r["caseId"])
    return [settings[k].summary() for k in sorted(settings)]


def load(results_path: Path, cases_path: Path) -> tuple[list[dict], dict[str, dict]]:
    results = json.loads(Path(results_path).read_text(encoding="utf-8"))["results"]
    cases = {c["case_id"]: c for c in json.loads(Path(cases_path).read_text(encoding="utf-8"))["cases"]}
    return results, cases


def render(rows: list[dict]) -> str:
    lines = [
        "| Min speech | Interruptions stopped | Misses (95% upper) | Backchannels stopped | Decision latency p50 / p95 | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['min_speech_ms']} ms | {r['stopped']} of {r['valid_interruptions']} | "
            f"{r['misses']} of {r['valid_interruptions']} ({r['miss_rate_95'][1]:.0%}) | {r['false_stops']} of {r['valid_backchannels']} | "
            f"{r['decision_latency_p50_ms']} / {r['decision_latency_p95_ms']} ms | {r['verdict']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    results, cases = load(Path(sys.argv[1]), Path(sys.argv[2]))
    rows = score(results, cases)
    print(render(rows))
    for r in rows:
        for reason in r["reasons"]:
            print(f"  {r['min_speech_ms']} ms: {reason}")
