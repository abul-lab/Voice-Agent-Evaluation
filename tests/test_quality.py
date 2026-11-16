"""Quality measures: failure categories, completion, red-team success, latency budget, regression gate and the judge."""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.evaluation import analysis
from voiceshop.evaluation.judge import (
    Completion,
    JudgeError,
    OllamaBackend,
    calibrate,
    judge_report,
    parse_score,
)
from voiceshop.evaluation.regression import compare, make_baseline

DATA = Path(__file__).resolve().parents[1] / "data" / "catalog" / "outdoor_store.json"


def _run(sid, passed, tags=(), checks=(), turns=4, latency=(10.0,), conversation=()):
    return {
        "scenario_id": sid,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "tags": list(tags),
        "language": "en",
        "checks": [{"name": n, "passed": ok, "detail": ""} for n, ok in checks],
        "metrics": {"turns": turns, "agent_latency_ms": list(latency)},
        "conversation": list(conversation),
    }


# ---------------------------------------------------------------- failure categories
def test_every_failed_check_is_put_in_a_category():
    records = [_run("a", False, checks=[("routing_correct", False), ("refund_policy", False),
                                        ("no_forbidden_act", True)]),
               _run("b", False, checks=[("verification_gate", False), ("made_up_check", False)])]
    out = analysis.categorise_failures(records)
    assert out["by_category"] == {"routing": 1, "policy": 1, "verification": 1, "other": 1}
    assert out["by_check"]["made_up_check"] == 1


# ---------------------------------------------------------------- completion by goal
def test_completion_and_handoff_are_reported_per_goal():
    records = [
        _run("s1", True, tags=["shopping"], checks=[("routing_correct", True)], turns=5),
        _run("s2", False, tags=["shopping"], checks=[("routing_correct", True)], turns=7),
        _run("r1", True, tags=["refund"], checks=[("routing_correct", False)], turns=3),
    ]
    out = analysis.completion_by_goal(records)
    assert out["shopping"]["cases"] == 2 and out["shopping"]["completion_rate"] == 0.5
    assert out["shopping"]["handoff_success_rate"] == 1.0
    assert out["shopping"]["mean_turns"] == 6.0
    assert out["refund"]["handoff_success_rate"] == 0.0


# ---------------------------------------------------------------- red team
def test_a_succeeded_attack_fails_the_security_gate():
    records = [_run("att1", True, tags=["redteam", "policy_bypass"]),
               _run("att2", False, tags=["redteam"], checks=[("no_forbidden_act", False)])]
    sec = analysis.attack_success(records)
    assert sec["attacks"] == 2 and sec["succeeded"] == 1
    assert sec["attack_success_rate"] == 0.5 and sec["passed"] is False
    assert sec["cases"] == ["att2"]


def test_gate_fails_when_any_attack_succeeds_even_if_the_pass_rate_is_fine():
    records = [_run(f"ok{i}", True) for i in range(9)]
    records.append(_run("attack", False, tags=["redteam"], checks=[("no_forbidden_text", False)]))
    summary = {"runs": 10, "passed": 9, "failed": 1, "errors": 0, "pass_rate": 0.9, "pass_threshold": 0.9}
    out = analysis.analyse(records, summary)
    assert out["gates"]["pass_rate"] is True and out["gates"]["attack_success"] is False
    assert out["gate_passed"] is False


# ---------------------------------------------------------------- latency budget
def test_latency_budget_uses_the_95th_percentile():
    fast = [_run(f"f{i}", True, latency=(5.0, 6.0)) for i in range(5)]
    assert analysis.latency_gate(fast)["passed"] is True
    slow = [_run("slow", True, latency=tuple([900.0] * 10))]
    gate = analysis.latency_gate(slow)
    assert gate["passed"] is False and gate["p95_ms"] == 900.0


def test_stage_latency_comes_from_the_recorded_turns():
    conv = [{"speaker": "agent", "stages": {"route_ms": 1.0}}, {"speaker": "agent", "stages": {"route_ms": 3.0}}]
    out = analysis.stage_latency([_run("x", True, conversation=conv)])
    assert out["route_ms"]["n"] == 2 and out["route_ms"]["p50"] == 2.0


def test_the_router_reports_time_for_each_step(ctx):
    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    gw = ToolGateway(store, _NullAudit(), tenant="t", ledger=ctx.refunds)
    cond = FrontDeskRouter(catalog, store, gw, date(2026, 11, 16))
    reply = cond.respond("Where is my order NW-1002?")
    assert set(reply.stages) == {"understand_ms", "route_ms", "dispatch_ms"}
    assert all(v >= 0 for v in reply.stages.values())


class _NullAudit:
    def record(self, *args, **kwargs):
        return None


# ---------------------------------------------------------------- regression gate
def _report(cases):
    return {"batch_id": "b", "summary": {"pass_rate": sum(p for _, p in cases) / len(cases)},
            "runs": [_run(sid, p, checks=[("routing_correct", p)]) for sid, p in cases]}


def test_identical_run_passes_the_baseline():
    base = make_baseline(_report([("a", True), ("b", True)]))
    assert compare(base, _report([("a", True), ("b", True)])) == []


def test_a_case_that_used_to_pass_and_now_fails_is_a_regression():
    base = make_baseline(_report([("a", True), ("b", True)]))
    findings = compare(base, _report([("a", True), ("b", False)]))
    rules = {f.rule for f in findings}
    assert "case_regressed" in rules and "pass_rate_dropped" in rules
    assert any(f.scenario_id == "b" and "routing_correct" in f.detail for f in findings)


def test_a_case_removed_from_the_suite_is_reported():
    base = make_baseline(_report([("a", True), ("gone", True)]))
    findings = compare(base, _report([("a", True)]))
    assert [f.rule for f in findings] == ["case_missing"]


def test_a_new_case_is_not_a_finding():
    base = make_baseline(_report([("a", True)]))
    assert compare(base, _report([("a", True), ("new", True)])) == []


# ---------------------------------------------------------------- judge
class _FakeBackend:
    name = "fake"
    model = "fake-1"

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return Completion(text=self.texts.pop(0), prompt_tokens=10, output_tokens=5, latency_ms=1.0)


GOOD = json.dumps({"accuracy": 5, "helpfulness": 4, "tone": 5, "invented_facts": False, "reason": "fine"})


def test_judge_answer_must_follow_the_rubric():
    assert parse_score(GOOD).accuracy == 5
    for bad in ('{"accuracy": 9, "helpfulness": 4, "tone": 5, "invented_facts": false}',
                '{"accuracy": true, "helpfulness": 4, "tone": 5, "invented_facts": false}',
                '{"accuracy": 4, "helpfulness": 4, "tone": 5, "invented_facts": "no"}',
                "not json", "[1, 2]"):
        with pytest.raises(JudgeError):
            parse_score(bad)


def test_judge_report_scores_agent_turns_and_counts_parse_failures():
    conv = [
        {"seq": 1, "speaker": "agent", "text_spoken": "Hi", "text_heard": "Hi", "agent": "router"},
        {"seq": 2, "speaker": "customer", "text_spoken": "Tent?", "text_heard": "Tent?"},
        {"seq": 3, "speaker": "agent", "text_spoken": "Ridgeline is $189", "text_heard": "", "agent": "shopping"},
    ]
    report = {"runs": [{"scenario_id": "x", "conversation": conv}]}
    backend = _FakeBackend([GOOD, "garbage"])
    out = judge_report(report, backend, facts="Ridgeline $189")
    assert out["summary"]["turns_judged"] == 2 and out["summary"]["failures"] == 1
    assert out["summary"]["accuracy"] == 5.0
    assert out["summary"]["total_tokens"] == 15  # only the answered call reports tokens
    assert backend.calls == 2


def test_calibration_counts_agreement_within_one_point():
    items = [
        {"heard": "a", "reply": "b", "human": {"accuracy": 5, "helpfulness": 4, "tone": 5, "invented_facts": False}},
        {"heard": "c", "reply": "d", "human": {"accuracy": 1, "helpfulness": 1, "tone": 1, "invented_facts": True}},
    ]
    backend = _FakeBackend([GOOD, GOOD])  # the judge says accurate both times: agrees once only
    out = calibrate(backend, items, facts="")
    assert out["agreement"] == 0.5 and out["items"] == 2


def _ollama_reachable() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3):  # noqa: S310
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama is not running on localhost:11434")
def test_ollama_backend_integration_returns_a_completion():
    """Checks the call and the plumbing only, not the judge's accuracy."""
    backend = OllamaBackend(model="qwen2.5:7b", timeout_s=240)
    done = backend.complete('Reply with JSON only: {"ok": true}')
    assert done.text.strip() and done.latency_ms > 0  # the model answered; its content is not judged here


def test_an_existing_version_1_database_is_upgraded_in_place(tmp_path):
    """Databases created before the timing column existed must keep working after the upgrade."""
    import sqlite3

    from voiceshop.storage.db import SCHEMA_VERSION, migrate

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, tenant_id TEXT NOT NULL,
             actor TEXT NOT NULL, action TEXT NOT NULL, entity_id TEXT, detail TEXT NOT NULL DEFAULT '{}');
           CREATE TABLE runs (id TEXT PRIMARY KEY, batch_id TEXT, scenario_id TEXT NOT NULL, mode TEXT NOT NULL,
             source TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
             summary TEXT NOT NULL DEFAULT '{}');
           CREATE TABLE turns (run_id TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE, seq INTEGER NOT NULL,
             speaker TEXT NOT NULL, language TEXT NOT NULL, text_spoken TEXT NOT NULL, text_heard TEXT NOT NULL,
             act TEXT, intent TEXT, latency_ms REAL, created_at TEXT NOT NULL, agent TEXT, shop_version INTEGER,
             PRIMARY KEY (run_id, seq));
           PRAGMA user_version = 1;"""
    )
    conn.close()
    assert migrate(path) == SCHEMA_VERSION
    conn = sqlite3.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
    conn.close()
    assert "stages" in cols


def test_runs_left_running_by_a_crash_are_marked_as_interrupted_at_start_up(ctx):
    ctx.runs.create("crashed-1", "store_order_status_shipped", "rules", "test", None)
    ctx.runs.create("finished-1", "store_order_status_shipped", "rules", "test", None)
    ctx.runs.finish("finished-1", "passed", {"passed": True})
    assert ctx.runs.reconcile_interrupted() == 1
    crashed = ctx.runs.get("crashed-1")
    assert crashed["status"] == "error" and crashed["summary"]["error"] == "interrupted"
    assert ctx.runs.get("finished-1")["status"] == "passed"  # finished runs are left alone


def test_a_judge_result_with_failed_calls_is_not_written_unless_partial_is_allowed():
    from voiceshop.evaluation.judge import assert_complete

    assert_complete({"failures": 0, "items": 3})  # complete: fine
    with pytest.raises(JudgeError) as err:
        assert_complete({"failures": 2, "items": 36})
    assert "nothing was written" in str(err.value)


def test_the_token_bucket_allows_a_burst_then_refuses_until_refilled():
    from voiceshop.monitor.ratelimit import TokenBucket

    bucket = TokenBucket(per_minute=2)
    assert bucket.allow("alice", now=0.0) and bucket.allow("alice", now=0.0)
    assert bucket.allow("alice", now=0.0) is False  # burst used up
    assert bucket.allow("bob", now=0.0)  # other callers are not affected
    assert bucket.allow("alice", now=31.0)  # half a minute later one more request is allowed


def test_logs_never_carry_customer_emails_even_in_extra_fields_or_exceptions():
    import io
    import logging

    from voiceshop.logging_utils import JsonFormatter, RedactionFilter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    log = logging.getLogger("test.redaction")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    log.info("refund asked by maria.lopez@example.com", extra={"detail": {"email": "maria.lopez@example.com"}})
    try:
        raise ValueError("lookup failed for dan.okafor@example.com")
    except ValueError:
        log.exception("request failed")
    text = buf.getvalue()
    assert "maria.lopez" not in text and "dan.okafor" not in text
    assert "[email]" in text


def test_retention_removes_old_transcripts_and_keeps_the_audit_trail(ctx):
    ctx.runs.create("old-run-1", "store_order_status_shipped", "rules", "test", None)
    ctx.runs.finish("old-run-1", "passed", {"passed": True})
    ctx.audit.record("old-tenant", "test", "run.kept", "old-run-1")
    import sqlite3

    conn = sqlite3.connect(ctx.db_path)
    conn.execute("UPDATE runs SET created_at='2020-01-01T00:00:00' WHERE id='old-run-1'")
    conn.commit()
    conn.close()
    preview = ctx.runs.prune_older_than(365, dry_run=True)
    assert preview["runs"] == 1 and ctx.runs.get("old-run-1") is not None  # dry run removes nothing
    result = ctx.runs.prune_older_than(365)
    assert result["runs"] == 1 and ctx.runs.get("old-run-1") is None
    assert ctx.audit.list("old-tenant")  # the audit trail is kept


def test_bad_settings_are_reported_in_plain_words_before_start(tmp_path):
    from voiceshop.config import ConfigError, load_settings, problems

    with pytest.raises(ConfigError, match="whole number"):
        load_settings(env={"VOICESHOP_MAX_CONCURRENT_RUNS": "many"})
    settings = load_settings(env={"VOICESHOP_MAX_CONCURRENT_RUNS": "99", "VOICESHOP_LOG_LEVEL": "LOUD",
                                  "VOICESHOP_KEYS_FILE": str(tmp_path / "missing.json")})
    found = problems(settings)
    assert any("MAX_CONCURRENT_RUNS" in p for p in found)
    assert any("LOG_LEVEL" in p for p in found)
    assert problems(load_settings(env={"VOICESHOP_KEYS_FILE": str(tmp_path / "none.json")})) == []


def test_the_configuration_report_never_shows_secret_values():
    from voiceshop.config import describe, load_settings

    shown = describe(load_settings(env={"VOICESHOP_API_KEY": "super-secret-admin", "GEMINI_API_KEY": "g-secret-1"}))
    text = json.dumps(shown)
    assert "super-secret-admin" not in text and "g-secret-1" not in text
    assert shown["shared_admin_key"] == "set" and shown["gemini_key"] == "set"


def test_a_changed_model_or_falling_agreement_is_reported_as_drift():
    from voiceshop.evaluation.judge import drift

    base = {"judge": {"backend": "ollama", "model": "qwen2.5:7b"}, "agreement": 0.5, "items": 36, "failures": 0}
    assert drift(base, dict(base)) == []
    assert any("judge changed" in r for r in drift(base, {**base, "judge": {"backend": "ollama", "model": "qwen3:8b"}}))
    assert any("agreement fell" in r for r in drift(base, {**base, "agreement": 0.3}))
    assert drift(base, {**base, "agreement": 0.47}) == []  # within the tolerance
    assert any("incomplete" in r for r in drift(base, {**base, "failures": 4}))


def test_transcripts_are_kept_for_two_weeks_by_default(tmp_path):
    from voiceshop.config import describe, load_settings, problems

    settings = load_settings(env={"VOICESHOP_KEYS_FILE": str(tmp_path / "none.json")})
    assert settings.retention_days == 14
    assert describe(settings)["transcript_retention_days"] == 14
    bad = load_settings(env={"VOICESHOP_RETENTION_DAYS": "0", "VOICESHOP_KEYS_FILE": str(tmp_path / "none.json")})
    assert any("RETENTION_DAYS" in p for p in problems(bad))
