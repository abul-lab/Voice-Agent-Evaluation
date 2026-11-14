"""The speech check's timing and round-trip limit, exercised with a fake speech service (no network)."""

from __future__ import annotations

import io
import json
import wave

import pytest

from voiceshop.context import build_context
from voiceshop.speech import frontdesk_check


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400)
    return buf.getvalue()


class _FakeSpeech:
    """Stands in for the Gemini adapter: returns the caller's line exactly, and a short audio file."""

    def __init__(self, api_key, **_):
        self.stt_model = "fake-stt"
        self.tts_model = "fake-tts"

    def synthesize(self, text, voice="Kore"):
        return _wav()

    def transcribe(self, wav):
        return "Hello, I would like a refund for my tent, please."


@pytest.fixture
def fake_run(monkeypatch, tmp_path, settings):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(frontdesk_check, "GeminiSpeech", _FakeSpeech)
    monkeypatch.setattr(frontdesk_check, "OUT", tmp_path / "out")
    monkeypatch.setattr(frontdesk_check, "build_context",
                        lambda s, path=None: build_context(s, tmp_path / "speech.db"))
    return tmp_path / "out"


def test_the_round_trip_is_measured_and_reported(fake_run):
    assert frontdesk_check.main() == 0
    report = json.loads((fake_run / "report.json").read_text(encoding="utf-8"))
    timing = report["timing_ms"]
    assert set(timing) == {"speech_to_text_ms", "route_ms", "text_to_speech_ms", "round_trip_ms"}
    assert all(v >= 0 for v in timing.values())
    check = next(c for c in report["checks"] if c["name"] == "speech_round_trip")
    assert check["passed"] is True and f"limit {frontdesk_check.ROUND_TRIP_LIMIT_MS:.0f} ms" in check["detail"]
    assert (fake_run / "reply.wav").exists()


def test_a_slow_round_trip_fails_the_check(fake_run, monkeypatch):
    monkeypatch.setattr(frontdesk_check, "ROUND_TRIP_LIMIT_MS", -1.0)  # any real timing is over the limit
    assert frontdesk_check.main() == 1
    report = json.loads((fake_run / "report.json").read_text(encoding="utf-8"))
    assert next(c for c in report["checks"] if c["name"] == "speech_round_trip")["passed"] is False
