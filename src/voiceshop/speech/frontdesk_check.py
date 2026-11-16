"""One real-speech case for the Front Desk Specialist.

  1. the front desk greets the caller; the greeting is synthesised and kept as a WAV file
  2. the caller says a request; the recording is kept, and Gemini transcribes it (what the agent hears)
  3. the front desk routes the transcript; the routing must still be Refunds, with the expected handoff
  4. the word error rate of the transcript is compared with the limit

Run:  python -m voiceshop.speech.frontdesk_check
Needs GEMINI_API_KEY in the environment. Evidence is written to results/speech/frontdesk_case/.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.config import load_settings
from voiceshop.context import build_context
from voiceshop.simulation.asr import word_error_rate
from voiceshop.speech.gemini import GeminiSpeech, SpeechError, api_key_from_env

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "catalog" / "outdoor_store.json"
OUT = ROOT / "results" / "speech" / "frontdesk_case"
TODAY = date(2026, 11, 16)
CALLER_LINE = "Hello, I would like a refund for my tent, please."
WER_LIMIT = 0.10  # starting point for clean audio; the director sets the real gate
ROUND_TRIP_LIMIT_MS = 3000.0  # starting point: caller's audio in, agent's spoken reply out
EXPECT_INTRO = "I'll connect you with our refunds team."


def _plain(text: str) -> str:
    """Lower case, no punctuation: a comma or full stop in a transcript is not a recognition error."""
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", text.lower()).split())


def main() -> int:
    key = api_key_from_env()
    if not key:
        print("GEMINI_API_KEY is not set in the environment; nothing was run.", file=sys.stderr)
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    speech = GeminiSpeech(api_key=key)
    settings = load_settings()
    ctx = build_context(settings, ROOT / "results" / "speech_check.db")
    catalog, store = Catalog.load(DATA), StoreData(DATA)

    def new_front_desk() -> FrontDeskRouter:
        gateway = ToolGateway(store, ctx.audit, tenant="speech-frontdesk", ledger=ctx.refunds)
        return FrontDeskRouter(catalog, store, gateway, TODAY)

    try:
        greeting = new_front_desk().greet()
        (OUT / "greeting.wav").write_bytes(speech.synthesize(greeting.text))

        caller_wav = speech.synthesize(CALLER_LINE, voice="Puck")  # a different voice from the agent's
        (OUT / "caller.wav").write_bytes(caller_wav)
        t_stt0 = time.perf_counter()
        heard = speech.transcribe(caller_wav)
        t_stt1 = time.perf_counter()
    except SpeechError as exc:
        print(f"speech service error: {exc}", file=sys.stderr)
        return 3

    wer = word_error_rate(_plain(CALLER_LINE), _plain(heard))  # punctuation is not a spoken word
    checks: list[dict] = [{"name": "word_error_rate", "passed": wer <= WER_LIMIT,
                           "detail": f"{wer:.1%} (limit {WER_LIMIT:.0%})"}]

    t_route0 = time.perf_counter()
    reply_heard = new_front_desk().respond(heard)
    t_route1 = time.perf_counter()
    try:
        reply_wav = speech.synthesize(reply_heard.text, voice="Kore")  # the agent's spoken reply
    except SpeechError as exc:
        print(f"speech service error: {exc}", file=sys.stderr)
        return 3
    (OUT / "reply.wav").write_bytes(reply_wav)
    t_tts1 = time.perf_counter()
    reply_exact = new_front_desk().respond(CALLER_LINE)
    timing = {
        "speech_to_text_ms": round((t_stt1 - t_stt0) * 1000, 1),
        "route_ms": round((t_route1 - t_route0) * 1000, 1),
        "text_to_speech_ms": round((t_tts1 - t_route1) * 1000, 1),
        "round_trip_ms": round((t_tts1 - t_stt0) * 1000, 1),
    }
    checks.append({"name": "speech_round_trip", "passed": timing["round_trip_ms"] <= ROUND_TRIP_LIMIT_MS,
                   "detail": f"{timing['round_trip_ms']:.0f} ms (limit {ROUND_TRIP_LIMIT_MS:.0f} ms)"})
    checks.append({"name": "routed_to_refunds", "passed": reply_heard.agent == "refund",
                   "detail": f"heard -> {reply_heard.agent}; exact text -> {reply_exact.agent}"})
    checks.append({"name": "handoff_announced", "passed": EXPECT_INTRO in reply_heard.text,
                   "detail": reply_heard.text[:120]})
    same = reply_heard.agent == reply_exact.agent
    checks.append({"name": "same_route_as_exact_text", "passed": same,
                   "detail": "recognition did not change the routing" if same else "recognition changed the routing"})

    passed = all(c["passed"] for c in checks)
    report = {
        "case": "frontdesk_refund_handoff",
        "models": {"speech_to_text": speech.stt_model, "text_to_speech": speech.tts_model},
        "caller_line": CALLER_LINE,
        "heard": heard,
        "greeting": greeting.text,
        "agent_reply": reply_heard.text,
        "timing_ms": timing,
        "passed": passed,
        "checks": checks,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for c in checks:
        print(f"{'PASS' if c['passed'] else 'FAIL'}  {c['name']:<26} {c['detail']}")
    print(f"heard: {heard}")
    print(f"evidence: {OUT}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
