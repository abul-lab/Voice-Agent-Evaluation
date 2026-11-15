"""Phase 1 conversation metrics, checked on hand-built records so each definition is pinned down."""

from voiceshop.evaluation import analysis
from voiceshop.evaluation.conversation_metrics import conversation_metrics


def _turn(speaker, text="", act=None, latency=None):
    return {"speaker": speaker, "text_spoken": text if speaker == "agent" else "",
            "text_heard": text if speaker == "customer" else "", "act": act, "latency_ms": latency}


def _record(scenario, conversation, passed=True, checks=()):
    return {"scenario_id": scenario, "tags": [], "passed": passed, "checks": list(checks),
            "metrics": {}, "conversation": conversation}


def test_task_completion_is_the_share_of_cases_that_pass():
    records = [_record("a", [], passed=True), _record("b", [], passed=False), _record("c", [], passed=True)]
    out = conversation_metrics(records)["task_completion"]
    assert out == {"cases": 3, "completed": 2, "completion_rate": 0.6667}


def test_a_caller_correction_is_counted_per_caller_turn():
    convo = [
        _turn("agent", "Your order is 1001.", act="order_status"),
        _turn("customer", "No, I said 1002."),
        _turn("customer", "Thanks."),
    ]
    out = conversation_metrics([_record("a", convo)])["corrections"]
    assert out["count"] == 1
    assert out["caller_turns"] == 2
    assert out["rate_per_caller_turn"] == 0.5


def test_a_no_answering_a_confirmation_is_not_a_correction():
    convo = [
        _turn("agent", "Shall I place the order?", act="confirm_order"),
        _turn("customer", "No, not yet."),
    ]
    assert conversation_metrics([_record("a", convo)])["corrections"]["count"] == 0


def test_interruptions_are_reported_as_not_measurable_on_text():
    out = conversation_metrics([_record("a", [_turn("customer", "hi")])])["interruptions"]
    assert out["measurable"] is False
    assert out["count"] is None


def test_latency_percentiles_use_agent_turns_only():
    convo = [_turn("agent", "a", act="x", latency=100.0), _turn("customer", "b", latency=9999.0),
             _turn("agent", "c", act="y", latency=300.0)]
    out = conversation_metrics([_record("a", convo)])["latency"]
    assert out["turns"] == 2
    assert out["p50_ms"] == 200.0
    assert out["max_ms"] == 300.0


def test_an_escalation_without_an_expected_escalation_check_is_unexpected():
    expected = _record("e", [_turn("agent", "Connecting you.", act="escalate")],
                       checks=[{"name": "escalation_code", "passed": True}])
    unexpected = _record("u", [_turn("agent", "Connecting you.", act="care_handoff")])
    out = conversation_metrics([expected, unexpected, _record("n", [])])["escalations"]
    assert out["cases_escalated"] == 2
    assert out["escalation_rate"] == 0.6667
    assert out["unexpected"] == 1
    assert out["unexpected_cases"] == ["u"]


def test_empty_suite_gives_no_rates_rather_than_dividing_by_zero():
    out = conversation_metrics([])
    assert out["task_completion"]["completion_rate"] is None
    assert out["corrections"]["rate_per_caller_turn"] is None
    assert out["escalations"]["escalation_rate"] is None


def test_the_suite_analysis_carries_the_metrics_and_does_not_gate_on_them():
    records = [_record("a", [_turn("agent", "hi", act="greet", latency=10.0)], passed=True)]
    summary = {"pass_rate": 1.0, "pass_threshold": 0.9, "errors": 0, "runs": 1}
    out = analysis.analyse(records, summary)
    assert "conversation_metrics" in out
    assert out["gate_passed"] is True


def test_repair_turns_and_handovers_are_counted():
    convo = [_turn("agent", "x", act="out_of_scope"), _turn("agent", "y", act="offer_options"),
             _turn("agent", "z", act="recovery_handoff"), _turn("agent", "ok", act="recommend")]
    clean = _record("clean", [_turn("agent", "ok", act="recommend")])
    out = conversation_metrics([_record("a", convo), clean])["repairs"]
    assert out["repair_turns"] == 3
    assert out["cases_with_repair"] == 1
    assert out["cases_handed_over_by_repair"] == 1
    assert out["turns_per_repaired_case"] == 3.0
