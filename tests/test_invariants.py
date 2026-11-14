"""Conduct invariant: when a caller says goodbye, the agent must close the call."""

from datetime import datetime

from voiceshop.evaluation.invariants import dialogue_violations, is_caller_goodbye
from voiceshop.simulation.conversation import TurnRecord


def _turn(seq, speaker, text, act=None):
    return TurnRecord(seq=seq, speaker=speaker, language="en", text_spoken=text, text_heard=text, act=act,
                      intent=None, latency_ms=None, created_at=datetime(2026, 11, 16).isoformat())


def test_goodbye_is_recognised_in_common_phrasings():
    for phrase in ("Goodbye", "Thanks, that's all", "Nothing else, thank you", "bye"):
        assert is_caller_goodbye(phrase), phrase
    assert not is_caller_goodbye("Where is my order?")


def test_a_correct_close_has_no_violation():
    turns = [_turn(1, "customer", "That's all, thanks."), _turn(2, "agent", "Thanks for shopping.", act="close")]
    assert dialogue_violations(turns) == []


def test_answering_a_goodbye_with_a_new_action_is_a_violation():
    turns = [_turn(1, "customer", "Thanks, that's all."), _turn(2, "agent", "Your order has shipped.",
                                                                act="order_status")]
    violations = dialogue_violations(turns)
    assert len(violations) == 1 and "instead of closing" in violations[0]


def test_a_goodbye_that_starts_a_new_task_is_not_treated_as_a_goodbye():
    turns = [_turn(1, "customer", "Goodbye, actually, can I buy a tent?"),
             _turn(2, "agent", "Which tent would you like?", act="ask_product")]
    assert dialogue_violations(turns) == []
