"""Monitor HTTP API: security controls, configuration, history and the store suite endpoints."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData
from voiceshop.catalog.storerun import load_store_scenarios
from voiceshop.errors import VoiceShopError
from voiceshop.monitor.server import MonitorApp, make_server

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "northwind_outdoor.json"
SCENARIOS = ROOT / "scenarios" / "store_scenarios.json"
KEY = "test-key-123456"


def _bundle():
    return {
        "catalog": Catalog.load(DATA),
        "store": StoreData(DATA),
        "scenarios": load_store_scenarios(SCENARIOS),
        "shops": ShopSessions(),
    }


def _serve(app):
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return base, server


@pytest.fixture
def live(ctx, tmp_path):
    app = MonitorApp(ctx, api_key=KEY, max_concurrent_runs=2, max_body_bytes=4096,
                     store_bundle=_bundle(), settings_path=tmp_path / "monitor_settings.json")
    base, server = _serve(app)
    yield base, app
    server.shutdown()
    server.server_close()
    app.close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback test server
        return r.status, r.read(), dict(r.headers)


def _post(url, body, key=None, ctype="application/json"):
    headers = {"Content-Type": ctype}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_health_and_security_headers(live):
    base, _ = live
    status, body, headers = _get(base + "/healthz")
    assert status == 200 and json.loads(body)["status"] == "ok"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert "frame-ancestors" in headers.get("Content-Security-Policy", "")


def test_pages_and_scripts_are_served(live):
    base, _ = live
    for path in ("/", "/tests", "/history", "/config", "/shop"):
        status, _, _ = _get(base + path)
        assert status == 200, path
    for path in ("/app.js", "/voice.js", "/agents.js", "/shop.js", "/config.js"):
        status, _, _ = _get(base + path)
        assert status == 200, path


def test_mutating_api_needs_the_key(live):
    base, _ = live
    status, _ = _post(base + "/api/evals", {"test_type": "smoke"}, key=None)
    assert status == 401
    status, _ = _post(base + "/api/evals", {"test_type": "smoke"}, key="wrong-key-000000")
    assert status == 401


def test_mutating_api_disabled_without_configured_key(ctx):
    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=4096, store_bundle=_bundle())
    base, server = _serve(app)
    try:
        status, body = _post(base + "/api/evals", {"test_type": "smoke"}, key=None)
        assert status == 403 and body["error"]["code"] == "api_disabled"
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_non_loopback_bind_is_refused_without_a_key(ctx):
    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=4096, store_bundle=_bundle())
    with pytest.raises(VoiceShopError) as err:
        make_server(app, "0.0.0.0", 0)
    assert err.value.code == "unsafe_binding"
    app.close()


def test_local_no_key_is_refused_on_a_public_address(ctx):
    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=4096,
                     local_ui_no_key=True, store_bundle=_bundle())
    with pytest.raises(VoiceShopError) as err:
        make_server(app, "0.0.0.0", 0)
    assert err.value.code == "unsafe_binding"
    app.close()


def test_capabilities_list_scopes_and_never_secrets(live):
    base, _ = live
    status, raw, _ = _get(base + "/api/capabilities")
    body = json.loads(raw)
    assert status == 200
    assert body["domains"] == ["store"]
    assert body["agents"]["care"] == [] and "shopping" in body["agents"]
    assert KEY not in raw.decode()


def test_evals_rejects_unknown_test_type(live):
    base, _ = live
    status, body = _post(base + "/api/evals", {"test_type": "everything"}, key=KEY)
    assert status == 400 and body["error"]["code"] == "bad_request"


def test_evals_refused_when_the_store_is_not_configured(ctx):
    app = MonitorApp(ctx, api_key=KEY, max_concurrent_runs=1, max_body_bytes=4096)
    base, server = _serve(app)
    try:
        status, body = _post(base + "/api/evals", {"test_type": "smoke"}, key=KEY)
        assert status == 400 and body["error"]["code"] == "domain_unavailable"
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_scenarios_are_listed_with_their_test_types(live):
    base, _ = live
    status, raw, _ = _get(base + "/api/scenarios")
    items = json.loads(raw)["items"]
    assert status == 200 and len(items) == 40
    smoke = [i for i in items if "smoke" in i["test_types"]]
    targeted = [i for i in items if "targeted" in i["test_types"]]
    assert len(smoke) == 1 and len(targeted) == 3


def test_clear_history_needs_the_key_and_keeps_the_audit_trail(live):
    base, app = live
    run_id = "history-check-1"
    app.ctx.runs.create(run_id, "store_order_status_shipped", "rules", "test", None)
    app.ctx.runs.finish(run_id, "passed", {"passed": True})
    app.ctx.audit.record("t", "test", "history.check", run_id)
    status, _ = _post(base + "/api/history/clear", {}, key=None)
    assert status == 401
    status, body = _post(base + "/api/history/clear", {}, key=KEY)
    assert status == 200 and body["removed"] >= 1
    assert app.ctx.runs.get(run_id) is None  # run record removed
    assert app.ctx.audit.list("t")  # the audit trail is kept


def test_settings_default_then_save_and_validate(live):
    base, app = live
    status, raw, _ = _get(base + "/api/settings")
    body = json.loads(raw)
    assert status == 200 and body["storefront_url"] is None
    assert [a["id"] for a in body["voice_agents"]] == ["router", "shopping", "order", "refund", "care"]
    care = next(a for a in body["voice_agents"] if a["id"] == "care")
    assert care["scopes"] == []  # customer care uses no back-office tools

    status, _ = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"}, key=None)
    assert status == 401
    status, _ = _post(base + "/api/settings", {"storefront_url": "ftp://x/shop"}, key=KEY)
    assert status == 400
    status, body = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/other"}, key=KEY)
    assert status == 400 and "/shop" in body["error"]["message"]
    status, body = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"}, key=KEY)
    assert status == 200 and body["storefront_url"] == "http://127.0.0.1:9/shop"
    assert app.storefront_url == "http://127.0.0.1:9/shop"
    status, body = _post(base + "/api/settings", {"storefront_url": ""}, key=KEY)
    assert status == 200 and body["storefront_url"] is None  # empty means the default


def test_settings_survive_a_restart(tmp_path):
    from voiceshop.monitor.server import _load_settings, _save_settings

    path = tmp_path / "monitor_settings.json"
    assert _load_settings(path) is None  # no file: default
    _save_settings(path, "http://localhost:8765/shop")
    assert _load_settings(path) == "http://localhost:8765/shop"
    path.write_text("{ not json", encoding="utf-8")
    assert _load_settings(path) is None  # a damaged file falls back to the default


def test_configuration_page_and_script_are_served(live):
    base, _ = live
    status, page, _ = _get(base + "/config")
    assert status == 200 and b"Storefront address" in page and b"Voice agents" in page
    assert b'href="/config"' in _get(base + "/")[1]  # the navigation links to it
    status, script, _ = _get(base + "/config.js")
    assert status == 200 and b"textContent" in script


def test_laptop_speaker_is_wired_to_the_conversation(live):
    base, _ = live
    _, voice, _ = _get(base + "/voice.js")
    _, app_js, _ = _get(base + "/app.js")
    assert b"SpeakerVoice" in voice and b"SpeakerVoice.speakSegments" in app_js
