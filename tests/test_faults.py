"""Fault injection: break a dependency on purpose and check the failure is contained and reported clearly."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from voiceshop.catalog.store import StoreData, ToolFault, ToolGateway
from voiceshop.evaluation.judge import JudgeError, calibrate, judge_report
from voiceshop.speech import frontdesk_check
from voiceshop.speech.gemini import SpeechError
from voiceshop.storage import db as dbmod
from voiceshop.storage.repository import AuditLog, RefundLedger, RunRepository

DATA = Path(__file__).resolve().parents[1] / "data" / "catalog" / "northwind_outdoor.json"


def test_a_failed_migration_is_rolled_back_and_the_version_is_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "migrate.db"
    dbmod.migrate(path)
    before = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    migrations = dict(dbmod._MIGRATIONS)
    migrations[before + 1] = ["CREATE TABLE ok_part (x INTEGER)", "THIS IS NOT SQL"]
    monkeypatch.setattr(dbmod, "_MIGRATIONS", migrations)
    monkeypatch.setattr(dbmod, "SCHEMA_VERSION", before + 1)
    with pytest.raises(sqlite3.Error):
        dbmod.migrate(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == before  # not half-upgraded
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='ok_part'").fetchone() is None
    conn.close()


def test_concurrent_writers_do_not_hit_database_locks(tmp_path):
    path = tmp_path / "busy.db"
    dbmod.migrate(path)
    runs = RunRepository(path)
    errors: list[str] = []

    def writer(n: int) -> None:
        try:
            run_id = f"busy-{n}"
            runs.create(run_id, "store_order_status_shipped", "rules", "test", None)
            for seq in range(1, 6):
                from voiceshop.simulation.conversation import TurnRecord

                runs.append_turn(run_id, TurnRecord(seq=seq, speaker="agent", language="en", text_spoken="x",
                                                    text_heard="x", act="greet", intent=None, latency_ms=1.0,
                                                    created_at="2026-11-14T10:00:00"))
            runs.finish(run_id, "passed", {"passed": True})
        except sqlite3.Error as exc:  # any lock error is a failure of this test
            errors.append(str(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], f"database errors under concurrent writes: {errors[:3]}"
    assert runs.counts()["passed"] == 12


def test_a_refund_back_office_outage_is_reported_and_never_guessed(tmp_path):
    store = StoreData(DATA)
    ledger = RefundLedger(tmp_path / "f.db")
    dbmod.migrate(tmp_path / "f.db")
    gw = ToolGateway(store, AuditLog(tmp_path / "f.db"), tenant="outage", ledger=ledger,
                     faults={"issue_refund": 1})
    with pytest.raises(ToolFault):
        gw.issue_refund("refund", store.orders["NW-1001"], 189, store.policy["version"])
    # the outage left no approval behind, so the retry is a fresh, correct approval
    _, duplicate = gw.issue_refund("refund", store.orders["NW-1001"], 189, store.policy["version"])
    assert duplicate is False


def test_a_speech_service_outage_stops_the_check_with_its_own_exit_code(tmp_path, monkeypatch):
    class DownSpeech:
        def __init__(self, api_key, **_):
            self.stt_model = self.tts_model = "fake"

        def synthesize(self, text, voice="Kore"):
            raise SpeechError("gemini-3.5-flash: service unavailable after 3 attempts (HTTP 503)")

        def transcribe(self, wav):
            raise SpeechError("unreachable")

    from voiceshop.context import build_context

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(frontdesk_check, "GeminiSpeech", DownSpeech)
    monkeypatch.setattr(frontdesk_check, "OUT", tmp_path / "out")
    monkeypatch.setattr(frontdesk_check, "build_context",
                        lambda s, path=None: build_context(s, tmp_path / "speech.db"))
    assert frontdesk_check.main() == 3  # its own code: a service problem, not a failed check


class _AlwaysRateLimited:
    name = "fake"
    model = "fake-1"

    def complete(self, prompt):
        raise JudgeError("judge backend unavailable after retries (HTTP 429)")


def test_a_rate_limited_judge_scores_nothing_and_counts_every_failure():
    report = {"runs": [{"scenario_id": "x", "conversation": [
        {"seq": 1, "speaker": "customer", "text_spoken": "hi", "text_heard": "hi"},
        {"seq": 2, "speaker": "agent", "text_spoken": "hello", "text_heard": "hello", "agent": "router"},
    ]}]}
    out = judge_report(report, _AlwaysRateLimited(), facts="")
    assert out["summary"]["turns_judged"] == 1 and out["summary"]["failures"] == 1
    assert out["summary"]["accuracy"] is None  # nothing was scored, so there is no average to report


def test_a_judge_outage_during_calibration_is_counted_not_scored():
    items = [{"heard": "a", "reply": "b", "human": {"accuracy": 5, "helpfulness": 5, "tone": 5,
                                                    "invented_facts": False}}]
    out = calibrate(_AlwaysRateLimited(), items, facts="")
    assert out["failures"] == 1 and out["agreement"] == 0.0


def test_the_api_refuses_oversized_and_malformed_bodies_and_keeps_serving(ctx, tmp_path):
    import urllib.error
    import urllib.request

    from voiceshop.monitor.server import MonitorApp, make_server

    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=2048)
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        big = urllib.request.Request(base + "/shop/api/action", data=b"x" * 10_000, method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(big, timeout=5)  # noqa: S310
        assert err.value.code in (400, 413)
        bad = urllib.request.Request(base + "/shop/api/action", data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err2:
            urllib.request.urlopen(bad, timeout=5)  # noqa: S310
        assert err2.value.code == 400
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:  # noqa: S310
            assert json.loads(r.read())["status"] == "ok"  # still serving after bad input
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_a_crashing_scenario_is_recorded_as_an_error_with_its_traceback(ctx, monkeypatch, caplog):
    import logging

    from voiceshop.catalog import multiagent
    from voiceshop.catalog.model import Catalog
    from voiceshop.catalog.shop import ShopSessions
    from voiceshop.catalog.storerun import load_store_scenarios, run_store_scenario

    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    scenario = load_store_scenarios(Path(__file__).resolve().parents[1] / "scenarios" / "store_scenarios.json")[0]

    def boom(self, utterance):
        raise RuntimeError("simulated router bug")

    monkeypatch.setattr(multiagent.FrontDeskRouter, "respond", boom)
    with caplog.at_level(logging.ERROR, logger="voiceshop.storerun"):
        run = run_store_scenario(ctx, catalog, store, scenario, shops=ShopSessions())
    assert run.error and "simulated router bug" in run.error  # recorded, not raised into the suite
    failed = [r for r in caplog.records if r.name == "voiceshop.storerun" and r.exc_info]
    assert failed, "the failure was logged with its traceback"


def test_a_failed_case_keeps_its_error_text_in_the_saved_run(ctx, monkeypatch):
    # The monitor shows the reason a case stopped; the reason must survive being saved.
    from voiceshop.catalog import multiagent
    from voiceshop.catalog.model import Catalog
    from voiceshop.catalog.storerun import load_store_scenarios, run_store_suite

    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    scenario = load_store_scenarios(Path(__file__).resolve().parents[1] / "scenarios" / "store_scenarios.json")[0]

    def boom(self, utterance):
        raise RuntimeError("simulated routing timeout")

    monkeypatch.setattr(multiagent.FrontDeskRouter, "respond", boom)
    run_store_suite(ctx, catalog, store, [scenario], threshold=0.9, batch_id="fault-1")
    saved = [r for r in ctx.runs.list(limit=10) if r["batch_id"] == "fault-1"][0]
    assert saved["status"] == "error"
    assert "simulated routing timeout" in saved["summary"]["error"]
