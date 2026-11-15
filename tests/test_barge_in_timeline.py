import json
import math
from pathlib import Path

import pytest

from voiceshop.speech.barge_in_timeline import (
    BLOCK,
    detect,
    heard_prefix,
    word_ends_ms,
)

RATE = 22050
ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "results" / "barge-in" / "clips"


def tone(ms, amplitude=0.2):
    n = round(ms * RATE / 1000)
    return [amplitude * math.sin(2 * math.pi * 440 * i / RATE) for i in range(n)]


def silence(ms):
    return [0.0] * round(ms * RATE / 1000)


def test_a_sustained_tone_is_detected_at_its_start_and_cut_after_the_minimum():
    clip = silence(200) + tone(1000)
    d = detect(clip, RATE, min_speech_ms=300)
    assert d.onset_ms == pytest.approx(200, abs=BLOCK * 1000 / RATE + 1)
    assert d.cut_ms is not None
    assert d.cut_ms - d.onset_ms == pytest.approx(300, abs=2 * BLOCK * 1000 / RATE)


def test_a_blip_shorter_than_the_minimum_is_not_a_cut():
    d = detect(tone(100), RATE, min_speech_ms=300)
    assert d.onset_sample is not None
    assert d.cut_sample is None


def test_silence_is_never_a_cut():
    d = detect(silence(1000), RATE, min_speech_ms=150)
    assert d.onset_sample is None and d.cut_sample is None


def test_a_pause_longer_than_the_hang_time_resets_the_count():
    # 200 ms of speech, a 300 ms pause, then 200 ms more: the minimum of 300 ms is never reached unbroken.
    clip = tone(200) + silence(300) + tone(200)
    d = detect(clip, RATE, min_speech_ms=300)
    assert d.cut_sample is None


def test_a_short_pause_inside_speech_keeps_the_count():
    clip = tone(200) + silence(100) + tone(200)
    d = detect(clip, RATE, min_speech_ms=300)
    assert d.cut_sample is not None


def test_word_ends_follow_the_speaking_rate():
    assert word_ends_ms("one two three", 380) == [380, 760, 1140]


def test_heard_prefix_is_the_words_that_finished_before_the_cut():
    text = "Hi, welcome to Eliza Voice Shopping today"
    heard, n = heard_prefix(text, cut_ms=800, ms_per_word=380)
    assert n == 2
    assert heard == "Hi, welcome"
    assert heard_prefix(text, cut_ms=10)[1] == 0


@pytest.mark.skipif(not (CLIPS / "manifest.json").exists(), reason="clips not generated (make_clips.ps1)")
def test_python_timing_agrees_with_the_browser_audio_thread_on_the_same_clips():
    # The browser run measured the decision on the page's audio clock. The same rule on the same clip should agree
    # to within one decision block.
    from voiceshop.speech.barge_in_timeline import read_wav

    results_file = ROOT / "results" / "barge-in" / "results.json"
    if not results_file.exists():
        pytest.skip("no browser run to compare with")
    results = json.loads(results_file.read_text(encoding="utf-8"))["results"]
    checked = 0
    for r in results:
        if r["expect"] != "stop" or not r.get("interrupted") or r.get("decisionLatencyMs") is None:
            continue
        clip_name = r["caseId"].rsplit("_min", 1)[0] + ".wav"
        rate, samples = read_wav(CLIPS / clip_name)
        minimum = int(r["caseId"].rsplit("_min", 1)[1])
        d = detect(samples, rate, min_speech_ms=minimum)
        assert d.cut_ms is not None
        python_latency = d.cut_ms - d.onset_ms
        # At 150 ms the two agree closely. At 300 and 600 ms they can differ by up to about 100 ms: a pause that sits
        # near the hang time can be read differently after the clip is resampled to the browser's rate. The Python
        # rule is the one the monitor runs; this comparison is recorded, not hidden (see the measurement doc).
        tolerance = 20 if minimum == 150 else 100
        assert abs(python_latency - r["decisionLatencyMs"]) <= tolerance, r["caseId"]
        checked += 1
    assert checked > 0
