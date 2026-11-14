"""Voice-channel model: simulates speech-recognition errors on the customer's words.

The customer's *spoken* text is what the customer intended. The agent only sees the *heard* text,
which may contain confusions (homophones, near-misses) and dropped words. Keeping both lets the
evaluation separate speech errors from reasoning errors, and measure word error rate (WER).

This is a text-level model of ASR, not audio. Real audio/STT/TTS adapters plug in at the same
boundary (see ``voiceshop.simulation.conversation.Channel``).
"""

from __future__ import annotations

import random
import re

# Words the recogniser often confuses, for English callers (shopping and order vocabulary).
CONFUSIONS = {
    "en": {
        "two": "to", "ten": "then", "fifteen": "fifty", "fourteen": "forty", "tent": "tint",
        "refund": "re fund", "order": "border", "cart": "card", "track": "trek", "checkout": "check out",
        "stove": "stow", "backpack": "back pack", "bedroll": "bed roll",
    },
}
_TOKEN = re.compile(r"\S+")


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over words, divided by reference length (0.0 = identical)."""
    ref = reference.lower().split()
    hyp = hypothesis.lower().split()
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        cur = [i]
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return round(prev[-1] / len(ref), 4)


class AsrNoise:
    """Applies confusion and deletion errors to spoken text at a per-word error rate."""

    def __init__(self, rate: float, rng: random.Random) -> None:
        if not 0.0 <= rate <= 0.5:
            raise ValueError("asr error rate must be between 0 and 0.5")
        self.rate = rate
        self.rng = rng

    def transform(self, spoken: str, language: str) -> str:
        if self.rate == 0.0:
            return spoken
        table = CONFUSIONS.get(language, {})
        out: list[str] = []
        for token in _TOKEN.findall(spoken):
            bare = token.strip(".,!?¿¡;:").lower()
            if self.rng.random() >= self.rate:
                out.append(token)
                continue
            if bare in table and self.rng.random() < 0.7:
                out.append(table[bare])
            elif self.rng.random() < 0.5:
                continue  # deletion
            else:
                out.append(token)
        return " ".join(out) if out else spoken
