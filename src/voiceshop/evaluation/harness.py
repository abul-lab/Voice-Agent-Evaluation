"""Suite runner: executes scenarios, evaluates them, aggregates metrics and writes reports.

Exit policy (used by the CLI and CI): the suite passes when the share of fully passing runs is at
least the threshold given to the suite and no run errored.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

log = logging.getLogger("voiceshop.eval")


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return round(ordered[k], 2)


def aggregate(records: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    total = len(records)
    passed = sum(1 for r in records if r["passed"])
    errors = sum(1 for r in records if r["status"] == "error")
    latencies = [ms for r in records for ms in r["metrics"].get("agent_latency_ms", [])]
    wers = [r["metrics"]["wer"] for r in records if "wer" in r["metrics"]]
    turns = [r["metrics"]["turns"] for r in records if "turns" in r["metrics"]]

    check_rates: dict[str, float] = {}
    for name in sorted({c["name"] for r in records for c in r["checks"]}):
        results = [c["passed"] for r in records for c in r["checks"] if c["name"] == name]
        check_rates[name] = round(sum(results) / len(results), 4)

    by_lang: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "passed": 0})
    by_tag: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for r in records:
        for bucket, key in ((by_lang, r["language"]),) + tuple((by_tag, t) for t in r["tags"]):
            bucket[key]["total"] += 1
            bucket[key]["passed"] += int(r["passed"])

    pass_rate = round(passed / total, 4) if total else 0.0
    return {
        "runs": total,
        "passed": passed,
        "failed": total - passed - errors,
        "errors": errors,
        "pass_rate": pass_rate,
        "pass_threshold": threshold,
        "gate_passed": total > 0 and errors == 0 and pass_rate >= threshold,
        "check_pass_rates": check_rates,
        "by_language": dict(by_lang),
        "by_tag": dict(by_tag),
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "mean": round(statistics.fmean(latencies), 2) if latencies else None,
        },
        "turns": {"mean": round(statistics.fmean(turns), 2) if turns else None, "max": max(turns, default=None)},
        "word_error_rate": {"mean": round(statistics.fmean(wers), 4) if wers else None},
        "intent_accuracy": _intent_accuracy(records),
    }


def _intent_accuracy(records: list[dict[str, Any]]) -> float | None:
    checked = [r for r in records if r["metrics"].get("intent_checked")]
    if not checked:
        return None
    return round(sum(1 for r in checked if r["metrics"]["intent_correct"]) / len(checked), 4)


def write_reports(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "report.md").write_text(to_markdown(report), encoding="utf-8")


def to_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"# Evaluation report: {report['batch_id']}",
        "",
        f"Mode: **{report['mode']}** · Generated: {report['generated_at']}",
        "",
        f"**Gate: {'PASS' if s['gate_passed'] else 'FAIL'}** "
        f"(pass rate {s['pass_rate']:.1%}, threshold {s['pass_threshold']:.0%})",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Runs | {s['runs']} |",
        f"| Passed | {s['passed']} |",
        f"| Failed | {s['failed']} |",
        f"| Errors | {s['errors']} |",
        f"| Intent accuracy (first goal) | {_fmt_pct(s['intent_accuracy'])} |",
        f"| Mean word error rate (voice channel) | {s['word_error_rate']['mean']} |",
        f"| Agent latency p50 / p95 (ms) | {s['latency_ms']['p50']} / {s['latency_ms']['p95']} |",
        f"| Turns mean / max | {s['turns']['mean']} / {s['turns']['max']} |",
        "",
        "## Checks",
        "",
        "| Check | Pass rate |",
        "|---|---|",
    ]
    lines += [f"| {name} | {rate:.1%} |" for name, rate in s["check_pass_rates"].items()]
    lines += _analysis_lines(s)
    lines += ["", "## By language", "", "| Language | Passed / total |", "|---|---|"]
    lines += [f"| {k} | {v['passed']} / {v['total']} |" for k, v in sorted(s["by_language"].items())]
    lines += ["", "## Runs", "", "| Scenario | Result | Turns | WER |", "|---|---|---|---|"]
    for r in report["runs"]:
        mark = "PASS" if r["passed"] else r["status"].upper()
        lines.append(f"| {r['scenario_id']} | {mark} | {r['metrics'].get('turns', '-')} | "
                     f"{r['metrics'].get('wer', '-')} |")
    if report["failures"]:
        lines += ["", "## Failures", ""]
        for f in report["failures"]:
            lines.append(f"### {f['scenario_id']}")
            for c in f["failed_checks"]:
                lines.append(f"- **{c['name']}**: {c['detail']}")
            lines.append("")
    return "\n".join(lines) + "\n"


def _analysis_lines(s: dict[str, Any]) -> list[str]:
    """Sections added by the store analysis. Absent for reports that were written without it."""
    if "gates" not in s:
        return []
    lines = ["", "## Release gates", "", "| Gate | Result |", "|---|---|"]
    lines += [f"| {name} | {'PASS' if ok else 'FAIL'} |" for name, ok in s["gates"].items()]
    sec, lat = s["security"], s["latency_gate"]
    lines += ["", f"Red team: {sec['succeeded']} of {sec['attacks']} attacks succeeded "
              f"(limit {sec['limit']:.0%}). Agent latency p95 {lat['p95_ms']} ms (budget {lat['budget_ms']:.0f} ms).",
              "", "## Failure categories", ""]
    cats = s["failure_analysis"]["by_category"]
    lines += ["| Category | Failed checks |", "|---|---|"] + [f"| {k} | {v} |" for k, v in cats.items()]
    if not cats:
        lines.append("| none | 0 |")
    lines += ["", "## Completion by goal", "", "| Goal | Cases | Completed | Completion | Handoff right | Mean turns |",
              "|---|---|---|---|---|---|"]
    for goal, c in s["completion"].items():
        lines.append(f"| {goal} | {c['cases']} | {c['completed']} | {c['completion_rate']:.0%} | "
                     f"{_fmt_pct(c['handoff_success_rate'])} | {c['mean_turns']} |")
    lines += ["", "## Latency by step (ms)", "", "| Step | p50 | p95 | Samples |", "|---|---|---|---|"]
    lines += [f"| {k} | {v['p50']} | {v['p95']} | {v['n']} |" for k, v in s["stage_latency_ms"].items()]
    return lines


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"
