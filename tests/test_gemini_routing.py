"""Gemini as the front desk's router. No network: the model's answers are faked at the request boundary."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from voiceshop.catalog import route_gemini
from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.route_gemini import GeminiRouteClassifier
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.net import ServiceError
from voiceshop.storage.repository import AuditLog, RefundLedger

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "northwind_outdoor.json"


def _answer(team_json: str):
    return {"candidates": [{"content": {"parts": [{"text": team_json}]}}]}


def test_the_classifier_reads_a_team_and_none(monkeypatch):
    replies = iter([_answer('{"team": "refund"}'), _answer('{"team": "none"}')])
    monkeypatch.setattr(route_gemini, "post_json", lambda *a, **k: next(replies))
    c = GeminiRouteClassifier("fake-key")
    assert c.classify("I want my money back") == "refund"
    assert c.classify("hello there") is None
    assert c.calls == 2


@pytest.mark.parametrize("bad", ['{"team": "magic"}', "not json", "{}"])
def test_an_unusable_answer_is_an_error_not_a_guess(monkeypatch, bad):
    monkeypatch.setattr(route_gemini, "post_json", lambda *a, **k: _answer(bad))
    with pytest.raises(ServiceError):
        GeminiRouteClassifier("fake-key").classify("anything")


class _Fixed:
    """A classifier that always names one team."""

    def __init__(self, team):
        self.team = team

    def classify(self, utterance):
        return self.team


def _router(ctx, classifier):
    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    shops = ShopSessions()
    gw = ToolGateway(store, AuditLog(ctx.db_path), tenant="gemini-route", ledger=RefundLedger(ctx.db_path),
                     catalog=catalog, shops=shops)
    return FrontDeskRouter(catalog, store, gw, date(2026, 11, 16), sessions=shops, sid="gemini-route",
                           classifier=classifier)


def test_the_model_decides_the_team_but_not_the_rules(ctx):
    # A shopping sentence, sent to refunds by the model: the refund rules still apply (ask for the order first).
    reply = _router(ctx, _Fixed("refund")).respond("I'd like a tent for two people")
    assert reply.agent == "refund" and reply.act == "ask_order"


def test_without_a_classifier_the_rules_decide(ctx):
    reply = _router(ctx, None).respond("I'd like a tent for two people")
    assert reply.agent == "shopping"


def test_a_failed_model_call_fails_the_case(ctx, tmp_path):
    from voiceshop.catalog.storerun import load_store_scenarios, run_store_scenario

    class Down:
        def classify(self, utterance):
            raise ServiceError("service unavailable after 3 attempts (HTTP 503)")

    scenario = load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json")[0]
    run = run_store_scenario(ctx, Catalog.load(DATA), StoreData(DATA), scenario, shops=ShopSessions(),
                             mode="gemini", classifier=Down())
    assert run.error and "ServiceError" in run.error
    assert ctx.runs.get(run.run_id)["mode"] == "gemini"


def test_the_server_refuses_gemini_runs_without_a_key_and_reports_it(ctx, tmp_path):
    from voiceshop.catalog.storerun import load_store_scenarios
    from voiceshop.monitor.server import MonitorApp, make_server

    bundle = {"catalog": Catalog.load(DATA), "store": StoreData(DATA),
              "scenarios": load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json"),
              "shops": ShopSessions()}
    app = MonitorApp(ctx, api_key="test-key-123456", max_concurrent_runs=1, max_body_bytes=4096,
                     store_bundle=bundle)
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/api/capabilities", timeout=5) as r:  # noqa: S310
            assert json.loads(r.read())["gemini_routing"] is False

        def post(body):
            req = urllib.request.Request(base + "/api/evals", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-API-Key": "test-key-123456"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        status, body = post({"test_type": "smoke", "mode": "gemini"})
        assert status == 400 and body["error"]["code"] == "gemini_not_configured"
        status, body = post({"test_type": "smoke", "mode": "magic"})
        assert status == 400 and body["error"]["code"] == "bad_request"
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_routing_waits_are_short_and_bounded(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, **kwargs):
        seen.update(kwargs)
        return _answer('{"team": "care"}')

    monkeypatch.setattr(route_gemini, "post_json", fake_post)
    assert GeminiRouteClassifier("fake-key").classify("I want to complain") == "care"
    assert seen["timeout_s"] <= 15 and seen["attempts"] <= 2  # a slow model cannot hold a run for minutes


def test_an_unavailable_router_fails_the_suite_gate_and_is_never_a_pass(ctx):
    from voiceshop.catalog.storerun import load_store_scenarios, run_store_suite

    class Down:
        def classify(self, utterance):
            raise ServiceError("service unavailable after 2 attempts (HTTP 503)")

    scenarios = load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json")
    smoke = [s for s in scenarios if "smoke" in s.tags]
    report = run_store_suite(ctx, Catalog.load(DATA), StoreData(DATA), smoke, threshold=0.9,
                             tags=None, mode="gemini", classifier=Down(), shops=ShopSessions())
    summary = report["summary"]
    assert summary["errors"] == 1 and summary["passed"] == 0
    assert summary["gate_passed"] is False  # an error is never a pass, so the release gate fails
