"""Barge-in timing in Python, for the agent-driven barge-in test.

The decision follows the same rule as the browser lab's audio-thread processor (tools/barge_in/lab-worklet.js):
blocks of 128 frames, an energy threshold, a hang time before speech counts as ended, and a cut once the caller's
unbroken speech has lasted the minimum time. Here it runs over the caller's recorded clip, so the decision uses real
audio. What is modelled is the agent's speech: each word takes a fixed time, so the moment a caller interrupts is
known exactly, and the words heard before the cut are the words that ended before it.

Limits (also stated in the report): the detector is an energy threshold, not a trained model; the agent's speaking
rate is a model; there is no echo and no telephone network.
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

BLOCK = 128  # frames per decision block, the same as the browser processor
HANG_MS = 200
DEFAULT_THRESHOLD = 0.02
MS_PER_WORD = 380  # agent speaking rate (a model; see the module docstring)


@dataclass(frozen=True)
class Decision:
    onset_sample: int | None  # first block of the caller's speech, in samples from the start of the clip
    cut_sample: int | None  # block where the minimum unbroken speech was reached, or None if never
    rate: int

    @property
    def onset_ms(self) -> float | None:
        return None if self.onset_sample is None else self.onset_sample * 1000 / self.rate

    @property
    def cut_ms(self) -> float | None:
        return None if self.cut_sample is None else self.cut_sample * 1000 / self.rate


def read_wav(path: Path) -> tuple[int, list[float]]:
    """A 16-bit mono WAV as floats in [-1, 1]. Other formats are refused rather than guessed at."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: needs 16-bit mono audio")
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    ints = struct.unpack(f"<{len(raw) // 2}h", raw)
    return rate, [v / 32768 for v in ints]


def detect(samples: list[float], rate: int, *, min_speech_ms: int, threshold: float = DEFAULT_THRESHOLD) -> Decision:
    """Runs the rule over a clip that starts when the caller starts speaking (or in silence before it)."""
    hang_frames = round(HANG_MS * rate / 1000 / BLOCK) * BLOCK
    min_frames = round(min_speech_ms * rate / 1000 / BLOCK) * BLOCK
    onset = cut = None
    in_speech = False
    speech_frames = 0
    silent_frames = 0
    for start in range(0, len(samples), BLOCK):
        block = samples[start:start + BLOCK]
        level = math.sqrt(sum(v * v for v in block) / len(block))
        if level > threshold:
            silent_frames = 0
            if not in_speech:
                in_speech = True
                speech_frames = 0
                if onset is None:
                    onset = start
            speech_frames += len(block)
            if cut is None and speech_frames >= min_frames:
                cut = start
        else:
            silent_frames += len(block)
            if in_speech and silent_frames >= hang_frames:
                in_speech = False
    return Decision(onset, cut, rate)


def word_ends_ms(text: str, ms_per_word: int = MS_PER_WORD) -> list[float]:
    """When each word of the agent's reply finishes, counting from the start of the reply."""
    words = text.split()
    return [(i + 1) * ms_per_word for i in range(len(words))]


def heard_prefix(text: str, cut_ms: float, ms_per_word: int = MS_PER_WORD) -> tuple[str, int]:
    """The words of the reply that finished before the caller's interruption cut it off."""
    words = text.split()
    ends = word_ends_ms(text, ms_per_word)
    heard = [w for w, end in zip(words, ends, strict=True) if end <= cut_ms]
    return " ".join(heard), len(heard)
