"""Barge-in inside a real conversation, for the agent-driven barge-in test.

A scenario can say: "interrupt the agent's Nth reply, after its Mth word, with this caller clip". The wrappers below do
that without changing the conversation loop's rules:

- ``BargeInAgent`` wraps the real agent. When the interrupted reply comes back, it judges the caller's clip with the
  timing rule (``barge_in_timeline``). If the rule cuts, the reply is marked with the words that were heard.
- ``BargeInCaller`` wraps the scripted caller. After a cut, its next line is the interruption itself, so the agent
  has to answer what was said, not carry on as if the reply had been heard in full.

A backchannel that the rule ignores changes nothing in the conversation. A backchannel the rule cuts on is a false
stop, and the conversation continues from it, which is what a real customer would experience.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voiceshop.simulation.conversation import CustomerUtterance
from voiceshop.speech.barge_in_timeline import (
    MS_PER_WORD,
    detect,
    heard_prefix,
    read_wav,
    word_ends_ms,
)

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CLIPS = ROOT / "results" / "barge-in" / "clips"
BUNDLED_CLIPS = ROOT / "data" / "barge_in" / "clips"


class BargeInSetupError(RuntimeError):
    """The clips for a barge-in scenario are missing or unreadable. The run is recorded as an error."""


@dataclass(frozen=True)
class BargeInSpec:
    turn: int  # which agent reply is interrupted: 1 is the reply to the caller's first line
    interrupt_after_words: int  # the caller starts speaking when this many words of the reply have been said
    clip: str  # a caller clip name from the clips manifest (make_clips.ps1)
    min_speech_ms: int  # unbroken speech the rule needs before it cuts

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BargeInSpec:
        return cls(
            turn=int(raw["turn"]),
            interrupt_after_words=int(raw["interrupt_after_words"]),
            clip=str(raw["clip"]),
            min_speech_ms=int(raw["min_speech_ms"]),
        )


@dataclass
class BargeInOutcome:
    turn: int
    clip: str
    kind: str  # "interrupt" or "backchannel", from the manifest
    expect: str  # "stop" or "continue"
    text: str  # what the caller said in the clip
    min_speech_ms: int
    valid: bool
    invalid_reason: str | None = None
    reply_text: str | None = None  # the whole reply the agent produced
    words_total: int = 0
    words_heard: int = 0
    heard: str | None = None  # the words the caller heard before the cut
    cut: bool = False
    caller_start_ms: float | None = None  # when the caller started, on the reply's clock
    detected_ms: float | None = None  # when the rule first saw the caller's speech
    cut_ms: float | None = None  # when the agent's audio was cut
    decision_latency_ms: float | None = None  # from the caller's onset to the cut

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _manifest_entry(clips_dir: Path, clip: str) -> dict[str, Any]:
    manifest_path = clips_dir / "manifest.json"
    if not manifest_path.exists():
        raise BargeInSetupError(f"barge-in clips not found in {clips_dir}: run tools/barge_in/make_clips.ps1")
    entries = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    for entry in entries:
        if entry["clip"] == clip:
            return entry
    raise BargeInSetupError(f"clip {clip!r} is not in the manifest")


@dataclass
class BargeInAgent:
    """Wraps the real agent. Only the reply it is asked to interrupt is judged."""

    inner: Any
    spec: BargeInSpec
    clips_dir: Path = DEFAULT_CLIPS
    replies: int = 0
    outcome: BargeInOutcome | None = None
    _pending: bool = field(default=False, init=False)
    _entry: dict[str, Any] | None = field(default=None, init=False)
    _rate: int = field(default=0, init=False)
    _samples: list[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if (
            self.clips_dir == DEFAULT_CLIPS
            and not (self.clips_dir / "manifest.json").exists()
            and (BUNDLED_CLIPS / "manifest.json").exists()
        ):
            self.clips_dir = BUNDLED_CLIPS
        self._entry = _manifest_entry(self.clips_dir, self.spec.clip)
        path = self.clips_dir / self.spec.clip
        if not path.exists():
            raise BargeInSetupError(f"clip file missing: {path}")
        try:
            self._rate, self._samples = read_wav(path)
        except ValueError as exc:
            raise BargeInSetupError(str(exc)) from exc

    def greet(self) -> Any:
        return self.inner.greet()

    def respond(self, utterance: str) -> Any:
        reply = self.inner.respond(utterance)
        self.replies += 1
        if self.replies == self.spec.turn and not reply.terminal:
            self.outcome = self._judge(reply)
            if self.outcome.cut:
                reply = copy.copy(reply)
                reply.heard = self.outcome.heard  # the transcript records what was heard, not what was said
                self._pending = True
        return reply

    def take_interrupt(self) -> bool:
        """True once, right after a cut: the caller's next line is the interruption."""
        pending, self._pending = self._pending, False
        return pending

    def _judge(self, reply: Any) -> BargeInOutcome:
        entry = self._entry or {}
        base = dict(
            turn=self.spec.turn,
            clip=self.spec.clip,
            kind=entry.get("kind", "unknown"),
            expect=entry.get("expect", ""),
            text=entry.get("text", ""),
            min_speech_ms=self.spec.min_speech_ms,
        )
        ends = word_ends_ms(reply.text, MS_PER_WORD)
        words_total = len(ends)
        reply_ms = ends[-1] if ends else 0.0
        if self.spec.interrupt_after_words < 1 or self.spec.interrupt_after_words > words_total:
            return BargeInOutcome(**base, valid=False, invalid_reason="the reply is shorter than the interruption point",
                                  reply_text=reply.text, words_total=words_total)
        caller_start = ends[self.spec.interrupt_after_words - 1]
        if caller_start >= reply_ms:
            return BargeInOutcome(**base, valid=False, invalid_reason="the caller spoke after the reply had ended",
                                  reply_text=reply.text, words_total=words_total)
        decision = detect(self._samples, self._rate, min_speech_ms=self.spec.min_speech_ms)
        out = BargeInOutcome(**base, valid=True, reply_text=reply.text, words_total=words_total,
                             caller_start_ms=caller_start)
        if decision.onset_ms is not None:
            out.detected_ms = caller_start + decision.onset_ms
        if decision.cut_ms is None:
            out.heard = reply.text  # the reply was not cut: it was heard in full
            out.words_heard = words_total
            return out
        cut_ms = caller_start + decision.cut_ms
        heard, n = heard_prefix(reply.text, cut_ms, MS_PER_WORD)
        out.cut = True
        out.cut_ms = cut_ms
        out.heard = heard
        out.words_heard = n
        out.decision_latency_ms = decision.cut_ms - decision.onset_ms
        return out


@dataclass
class BargeInCaller:
    """Wraps the scripted caller. After a cut, its next line is the interruption."""

    inner: Any
    agent: BargeInAgent

    def open(self) -> CustomerUtterance:
        return self.inner.open()

    def respond(self, act: str, params: dict[str, Any], language: str) -> CustomerUtterance:
        if self.agent.take_interrupt() and self.agent.outcome is not None:
            outcome = self.agent.outcome
            return CustomerUtterance(text=outcome.text, language=language,
                                     intent_label="interrupt", is_goodbye=False, is_abandon=False)
        return self.inner.respond(act, params, language)


def clips_dir_from_env() -> Path:
    env_dir = os.environ.get("VOICESHOP_BARGE_IN_CLIPS")
    if env_dir:
        return Path(env_dir)
    if (DEFAULT_CLIPS / "manifest.json").exists() or not (BUNDLED_CLIPS / "manifest.json").exists():
        return DEFAULT_CLIPS
    return BUNDLED_CLIPS
