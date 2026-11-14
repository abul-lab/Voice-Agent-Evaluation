"""Regression gate: a case that passed in the saved baseline must still pass.

The baseline is written from a clean suite run and committed. Each later run is compared with it:

  case_regressed    a case that passed in the baseline fails now
  case_missing      a baseline case is no longer in the suite (update the baseline in the same change)
  pass_rate_dropped the overall pass rate is below the baseline's

New cases are not findings: they are added to the baseline when it is next written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Finding:
    rule: str
    scenario_id: str | None
    detail: str


def make_baseline(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_batch": report["batch_id"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pass_rate": report["summary"]["pass_rate"],
        "cases": {
            r["scenario_id"]: {
                "passed": bool(r["passed"]),
                "checks": {c["name"]: bool(c["passed"]) for c in r["checks"]},
            }
            for r in report["runs"]
        },
    }


def compare(baseline: dict[str, Any], report: dict[str, Any]) -> list[Finding]:
    if baseline.get("schema_version") != 1:
        raise ValueError("unsupported baseline schema_version")
    current = {r["scenario_id"]: r for r in report["runs"]}
    findings: list[Finding] = []
    for sid, expected in sorted(baseline["cases"].items()):
        run = current.get(sid)
        if run is None:
            findings.append(Finding("case_missing", sid, "in the baseline but not in this run"))
            continue
        if expected["passed"] and not run["passed"]:
            failing = [c["name"] for c in run["checks"] if not c["passed"]]
            findings.append(Finding("case_regressed", sid, f"passed in the baseline; now fails: {', '.join(failing)}"))
    now = report["summary"]["pass_rate"]
    before = baseline.get("pass_rate")
    if before is not None and now < before - 1e-9:
        findings.append(Finding("pass_rate_dropped", None, f"pass rate {now:.1%} is below the baseline {before:.1%}"))
    return findings


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
