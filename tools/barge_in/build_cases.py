"""Builds the barge-in case list for the browser lab (tools/barge_in/run.mjs).

Each case pairs one caller clip with one minimum-speech setting and an expected result: "stop" for a real
interruption, "continue" for a backchannel. The clips and their manifest come from tools/barge_in/make_clips.ps1.
The lab plays the agent's reply and the caller's clip on the page's audio clock, so no microphone file is needed.
See docs/barge-in-measurement.md for what this does and does not measure.

Run after make_clips.ps1. Output goes to results/barge-in/cases.json (git-ignored).
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "barge-in"
CLIPS = OUT / "clips"

MIN_SPEECH_MS = (150, 300, 600)
LEAD_SEC = 3.0  # the caller starts 3 s after the agent; the lab uses this only to schedule the clip


def main() -> None:
    manifest = json.loads((CLIPS / "manifest.json").read_text(encoding="utf-8-sig"))
    callers = [m for m in manifest if m["kind"] in ("interrupt", "backchannel")]
    cases = []
    for m in callers:
        stem = m["clip"].removesuffix(".wav")
        for min_speech in MIN_SPEECH_MS:
            cases.append({
                "case_id": f"{stem}_min{min_speech}",
                "kind": m["kind"],
                "expect": m["expect"],
                "clip": m["clip"],
                "voice": m["voice"],
                "text": m["text"],
                "lead_sec": LEAD_SEC,
                "min_speech_ms": min_speech,
            })
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cases.json").write_text(json.dumps({"cases": cases}, indent=2), encoding="utf-8")
    n_stop = sum(1 for m in callers if m["expect"] == "stop")
    print(f"wrote {len(cases)} cases ({n_stop} interruption and {len(callers) - n_stop} backchannel clips "
          f"x {len(MIN_SPEECH_MS)} settings) to {OUT / 'cases.json'}")


if __name__ == "__main__":
    main()
