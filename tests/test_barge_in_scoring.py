from voiceshop.evaluation.barge_in_scoring import percentile, render, score, wilson


def _case(case_id, expect, min_speech):
    return case_id, {"case_id": case_id, "expect": expect, "min_speech_ms": min_speech}


def _result(case_id, interrupted, latency=None, valid=True, error=None):
    return {
        "caseId": case_id,
        "valid": valid,
        "interrupted": interrupted,
        "decisionLatencyMs": latency,
        "error": error,
    }


def test_wilson_matches_the_known_zero_success_bound():
    # 0 misses in 73 cases: upper bound just under 5%. This is why 73 is the minimum.
    _, high = wilson(0, 73)
    assert high <= 0.05
    _, high = wilson(0, 72)
    assert high > 0.05


def test_percentile_interpolates():
    assert percentile([100, 200, 300, 400], 0.5) == 250
    assert percentile([], 0.5) is None


def test_interruptions_and_backchannels_are_counted_separately_per_setting():
    cases = dict([
        _case("i1", "stop", 300), _case("i2", "stop", 300),
        _case("b1", "continue", 300), _case("b2", "continue", 300),
    ])
    results = [
        _result("i1", True, 310), _result("i2", False),
        _result("b1", True, 305), _result("b2", False),
    ]
    (row,) = score(results, cases)
    assert row["valid_interruptions"] == 2
    assert row["stopped"] == 1
    assert row["misses"] == 1
    assert row["miss_cases"] == ["i2"]
    assert row["valid_backchannels"] == 2
    assert row["false_stops"] == 1
    assert row["false_stop_cases"] == ["b1"]
    assert row["decision_latency_p50_ms"] == 310


def test_invalid_and_errored_cases_are_excluded_and_counted():
    cases = dict([_case("i1", "stop", 150), _case("i2", "stop", 150)])
    results = [_result("i1", True, 150), _result("i2", True, 150, valid=False)]
    (row,) = score(results, cases)
    assert row["valid_interruptions"] == 1
    assert row["invalid_cases"] == 1

    results = [_result("i1", True, 150), _result("i2", False, error="boom")]
    (row,) = score(results, cases)
    assert row["invalid_cases"] == 1
    assert row["valid_interruptions"] == 1


def test_a_small_sample_can_show_it_works_but_the_interval_is_wide():
    cases = {f"i{k}": {"case_id": f"i{k}", "expect": "stop", "min_speech_ms": 150} for k in range(4)}
    results = [_result(f"i{k}", True, 150) for k in range(4)]
    (row,) = score(results, cases)
    assert row["verdict"] == "works"
    assert row["miss_rate_95"][1] > 0.3  # four cases cannot rule out a large miss rate


def test_latency_over_budget_fails_the_gate():
    cases = {f"i{k}": {"case_id": f"i{k}", "expect": "stop", "min_speech_ms": 600} for k in range(80)}
    results = [_result(f"i{k}", True, 600) for k in range(80)]
    (row,) = score(results, cases)
    assert row["misses"] == 0
    assert row["verdict"] == "does not work"
    assert any("budget" in r for r in row["reasons"])


def test_a_setting_works_when_it_stops_every_interruption_and_no_backchannel():
    cases = {f"i{k}": {"case_id": f"i{k}", "expect": "stop", "min_speech_ms": 150} for k in range(3)}
    cases.update({f"b{k}": {"case_id": f"b{k}", "expect": "continue", "min_speech_ms": 150} for k in range(3)})
    results = [_result(f"i{k}", True, 160) for k in range(3)] + [_result(f"b{k}", False) for k in range(3)]
    (row,) = score(results, cases)
    assert row["verdict"] == "works", row["reasons"]


def test_an_unknown_case_is_an_error_not_a_silent_skip():
    try:
        score([_result("nope", True)], {})
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected a ValueError")


def test_render_makes_a_table():
    cases = dict([_case("i1", "stop", 150)])
    rows = score([_result("i1", True, 160)], cases)
    text = render(rows)
    assert text.startswith("| Min speech |")
    assert "150 ms" in text
