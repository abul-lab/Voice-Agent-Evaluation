"""Barge-in (roadmap 3.3): the caller speaks over the agent, and the agent stops talking.

This module holds the decision and the timing; it does not play or record audio. A real channel reports when
the caller starts speaking and when playback starts, and asks this module what the caller heard. With the flag off
(the default), playback always runs to the end and the caller waits, which is the current behaviour.

Timing is simulated from the reply text: each word takes ``ms_per_word`` milliseconds to say. Detection of the
caller's speech takes ``detection_ms``. Both are starting points to be replaced with measurements from a real channel.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MS_PER_WORD = 380
DEFAULT_DETECTION_MS = 150  # time for the channel to notice the caller is speaking
STOP_BUDGET_MS = 300  # from the caller starting to speak to the agent going quiet


@dataclass(frozen=True)
class Playback:
    """One agent reply being spoken from ``start_ms``."""

    text: str
    start_ms: int
    ms_per_word: int = DEFAULT_MS_PER_WORD

    @property
    def words(self) -> list[str]:
        return self.text.split()

    @property
    def end_ms(self) -> int:
        return self.start_ms + len(self.words) * self.ms_per_word


@dataclass(frozen=True)
class Interruption:
    """The agent was cut off. ``heard`` is what the caller actually heard of the reply."""

    caller_started_ms: int
    stopped_ms: int
    heard: str
    words_heard: int
    words_total: int

    @property
    def stop_latency_ms(self) -> int:
        return self.stopped_ms - self.caller_started_ms

    @property
    def within_budget(self) -> bool:
        return self.stop_latency_ms <= STOP_BUDGET_MS


class BargeIn:
    """Decides whether caller speech interrupts playback. Off means never interrupt."""

    def __init__(self, enabled: bool = False, detection_ms: int = DEFAULT_DETECTION_MS) -> None:
        self.enabled = enabled
        self.detection_ms = detection_ms

    def check(self, playback: Playback, caller_started_ms: int) -> Interruption | None:
        """Interruption if the caller starts speaking while the reply is playing and the channel notices
        before the reply would have ended. Otherwise None: the caller's speech waits for its own turn."""
        if not self.enabled:
            return None
        if not (playback.start_ms <= caller_started_ms < playback.end_ms):
            return None
        stopped = caller_started_ms + self.detection_ms
        if stopped >= playback.end_ms:
            return None  # the reply finished before the channel noticed: not an interruption
        ms = playback.ms_per_word
        words_heard = (stopped - playback.start_ms) // ms
        words = playback.words
        heard_words = words[:words_heard]
        return Interruption(
            caller_started_ms=caller_started_ms,
            stopped_ms=stopped,
            heard=" ".join(heard_words),
            words_heard=len(heard_words),
            words_total=len(words),
        )
