from dataclasses import dataclass, field

import pytest

from voiceshop.simulation.conversation import CustomerUtterance
from voiceshop.speech.barge_in_turns import (
    BUNDLED_CLIPS,
    DEFAULT_CLIPS,
    BargeInAgent,
    BargeInCaller,
    BargeInSetupError,
    BargeInSpec,
)

HAS_CLIPS = (DEFAULT_CLIPS / "manifest.json").exists() or (BUNDLED_CLIPS / "manifest.json").exists()

REPLY = "Here is the Ridgeline two person tent for you at one hundred and eighty nine dollars today"


@dataclass
class StubReply:
    text: str
    act: str = "recommend"
    terminal: bool = False
    params: dict = field(default_factory=dict)
    language: str = "en"
    intent: str | None = None
    latency_ms: float = 1.0
    agent: str = "shopping"


class StubAgent:
    def __init__(self, text=REPLY):
        self.text = text
        self.asked = []

    def greet(self):
        return StubReply(text="Hello", act="greet", agent="router")

    def respond(self, utterance):
        self.asked.append(utterance)
        return StubReply(text=self.text)


class StubCaller:
    def open(self):
        return CustomerUtterance(text="I want a tent", language="en", intent_label="recommend")

    def respond(self, act, params, language):
        return CustomerUtterance(text="Thanks, bye", language=language, intent_label=None, is_goodbye=True)


def test_missing_clips_are_a_setup_error_that_says_how_to_fix_it(tmp_path):
    spec = BargeInSpec(turn=1, interrupt_after_words=4, clip="interrupt_david_1.wav", min_speech_ms=150)
    with pytest.raises(BargeInSetupError, match="make_clips"):
        BargeInAgent(StubAgent(), spec, clips_dir=tmp_path)


def test_the_default_clip_location_is_the_results_folder():
    assert DEFAULT_CLIPS.name == "clips"
    assert DEFAULT_CLIPS.parent.name == "barge-in"


@pytest.mark.skipif(not HAS_CLIPS, reason="clips not generated (make_clips.ps1)")
def test_an_interruption_cuts_the_reply_and_the_caller_says_it_next():
    spec = BargeInSpec(turn=1, interrupt_after_words=4, clip="interrupt_david_1.wav", min_speech_ms=150)
    agent = BargeInAgent(StubAgent(), spec)
    caller = BargeInCaller(StubCaller(), agent)

    reply = agent.respond("I want a tent")
    outcome = agent.outcome
    assert outcome.valid and outcome.cut
    assert outcome.kind == "interrupt" and outcome.expect == "stop"
    # The caller started after four words: the heard text is the first words that finished before the cut.
    words = REPLY.split()
    assert outcome.heard == " ".join(words[: outcome.words_heard])
    assert 4 <= outcome.words_heard < len(words)
    assert reply.heard == outcome.heard

    nxt = caller.respond("recommend", {}, "en")
    assert nxt.text == outcome.text
    assert nxt.intent_label == "interrupt"


@pytest.mark.skipif(not HAS_CLIPS, reason="clips not generated (make_clips.ps1)")
def test_a_backchannel_the_rule_ignores_leaves_the_conversation_unchanged():
    spec = BargeInSpec(turn=1, interrupt_after_words=4, clip="backchannel_david_1.wav", min_speech_ms=600)
    agent = BargeInAgent(StubAgent(), spec)
    caller = BargeInCaller(StubCaller(), agent)
    reply = agent.respond("I want a tent")
    assert agent.outcome.valid and not agent.outcome.cut
    assert agent.outcome.heard == REPLY  # the whole reply was heard
    assert not hasattr(reply, "heard")
    nxt = caller.respond("recommend", {}, "en")
    assert nxt.text == "Thanks, bye"  # the scripted caller carries on


@pytest.mark.skipif(not HAS_CLIPS, reason="clips not generated (make_clips.ps1)")
def test_a_caller_who_speaks_after_the_reply_ended_is_an_invalid_case():
    short = StubAgent(text="Done.")
    spec = BargeInSpec(turn=1, interrupt_after_words=4, clip="interrupt_david_1.wav", min_speech_ms=150)
    agent = BargeInAgent(short, spec)
    agent.respond("hi")
    assert agent.outcome.valid is False
    assert "shorter" in agent.outcome.invalid_reason
