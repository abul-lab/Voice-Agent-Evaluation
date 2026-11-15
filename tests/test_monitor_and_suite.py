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
    assert status == 200 and len(items) == 74
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
    from voiceshop.monitor.settings import _load_settings, _save_settings

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


def test_a_running_suite_is_stopped_and_drained_on_shutdown(ctx):
    app = MonitorApp(ctx, api_key=KEY, max_concurrent_runs=2, max_body_bytes=4096, store_bundle=_bundle())
    app.try_acquire()
    batch = app.submit_store_suite("smoke", pace_ms=150)
    import time as _t

    deadline = _t.monotonic() + 10
    while app.metrics.active_runs == 0 and _t.monotonic() < deadline:
        _t.sleep(0.02)
    assert app.drain(timeout_s=20) is True
    assert app.metrics.active_runs == 0
    assert batch  # the suite was started and then stopped
    app.close()


def _ring(tmp_path):
    from voiceshop.monitor.auth import KeyRing, hash_key

    keys = {"reader": ("viewer", "k-viewer-000000001"), "tester": ("operator", "k-operator-00000002"),
            "lead": ("admin", "k-admin-0000000003")}
    entries = [{"name": n, "role": r, "sha256": hash_key(k)} for n, (r, k) in keys.items()]
    return KeyRing(entries), {n: k for n, (r, k) in keys.items()}


def _named_app(ctx, tmp_path):
    ring, keys = _ring(tmp_path)
    app = MonitorApp(ctx, keys=ring, max_concurrent_runs=2, max_body_bytes=4096, store_bundle=_bundle(),
                     settings_path=tmp_path / "monitor_settings.json")
    base, server = _serve(app)
    return app, base, server, keys


def test_roles_decide_who_can_start_suites_and_change_settings(ctx, tmp_path):
    app, base, server, keys = _named_app(ctx, tmp_path)
    try:
        status, body = _post(base + "/api/evals", {"test_type": "smoke"}, key=keys["reader"])
        assert status == 403 and body["error"]["code"] == "forbidden"  # viewer cannot start a suite
        status, body = _post(base + "/api/evals", {"test_type": "bogus"}, key=keys["tester"])
        assert status == 400  # operator passes the role check; the request itself is what is wrong
        status, body = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"},
                             key=keys["tester"])
        assert status == 403  # operators cannot change settings
        status, body = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"},
                             key=keys["lead"])
        assert status == 200
        status, _ = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"}, key="wrong-key-00000")
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_settings_changes_are_recorded_against_the_person_who_made_them(ctx, tmp_path):
    app, base, server, keys = _named_app(ctx, tmp_path)
    try:
        status, _ = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"}, key=keys["lead"])
        assert status == 200
        events = app.ctx.audit.list("monitor")
        changed = [e for e in events if e["action"] == "settings.changed"]
        assert changed and changed[-1]["actor"] == "lead"
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_a_revoked_key_is_refused(tmp_path):
    from voiceshop.monitor.auth import KeyRing, hash_key, load_entries, save_keys

    path = tmp_path / "api_keys.json"
    save_keys(path, [{"name": "gone", "role": "admin", "sha256": hash_key("k-gone-0000000000001")}])
    assert KeyRing(load_entries(path)).authenticate("k-gone-0000000000001").name == "gone"
    save_keys(path, [])  # revoked: the entry is removed from the key file
    assert KeyRing(load_entries(path)).authenticate("k-gone-0000000000001") is None


def test_the_key_file_holds_hashes_never_the_keys(tmp_path):
    from voiceshop.monitor.auth import KeyRing, hash_key, load_entries, new_key, save_keys

    key = new_key()
    path = tmp_path / "api_keys.json"
    save_keys(path, [{"name": "qe-lead", "role": "admin", "sha256": hash_key(key)}])
    text = path.read_text(encoding="utf-8")
    assert key not in text
    assert KeyRing(load_entries(path)).authenticate(key).role == "admin"


def test_damaged_key_files_are_refused(tmp_path):
    from voiceshop.monitor.auth import KeyFileError, KeyRing

    with pytest.raises(KeyFileError):
        KeyRing([{"name": "x", "role": "root", "sha256": "0" * 64}])  # unknown role
    with pytest.raises(KeyFileError):
        KeyRing([{"name": "x", "role": "admin", "sha256": "not-a-hash"}])


def test_changes_are_rate_limited_per_person(ctx, tmp_path):
    ring, keys = _ring(tmp_path)
    app = MonitorApp(ctx, keys=ring, max_concurrent_runs=1, max_body_bytes=4096, store_bundle=_bundle(),
                     settings_path=tmp_path / "monitor_settings.json", changes_per_minute=1)
    base, server = _serve(app)
    try:
        first, _ = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"}, key=keys["lead"])
        second, body = _post(base + "/api/settings", {"storefront_url": "http://127.0.0.1:9/shop"}, key=keys["lead"])
        assert first == 200 and second == 429 and body["error"]["code"] == "rate_limited"
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_behind_a_trusted_proxy_each_client_is_limited_separately(ctx, tmp_path):
    ring, keys = _ring(tmp_path)
    app = MonitorApp(ctx, keys=ring, max_concurrent_runs=1, max_body_bytes=4096, store_bundle=_bundle(),
                     settings_path=tmp_path / "monitor_settings.json", attempts_per_minute=2, trust_proxy=True)
    base, server = _serve(app)
    try:
        def attempt(client):
            req = urllib.request.Request(base + "/api/evals", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "X-Forwarded-For": client})
            try:
                with urllib.request.urlopen(req, timeout=5):  # noqa: S310
                    return 200
            except urllib.error.HTTPError as err:
                return err.code

        assert [attempt("10.0.0.1") for _ in range(3)][-1] == 429  # one client used up its attempts
        assert attempt("10.0.0.2") == 401  # a different client is not affected
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_the_api_description_matches_the_routes_the_server_answers(live):
    """Drift check: every path in deploy/openapi.json must be a real route (not 'no such route')."""
    import re

    base, _ = live
    spec = json.loads((ROOT / "deploy" / "openapi.json").read_text(encoding="utf-8"))
    for path, methods in spec["paths"].items():
        for method in methods:
            url = base + re.sub(r"\{[^}]+\}", "probe-run-1", path)
            req = urllib.request.Request(url, method=method.upper(), data=b"{}" if method != "get" else None,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
                    code = r.status
                    # A live event stream never ends: its status is enough, the body is not read.
                    text = b"" if path.endswith("/stream") else r.read()
            except urllib.error.HTTPError as err:
                code, text = err.code, err.read()
            is_missing = code == 404 and b'"not_found"' in text and b"no such route" in text
            assert not is_missing, f"{method.upper()} {path} is in the description but the server has no such route"


def test_a_suite_start_is_audited_with_the_request_id_that_started_it(ctx, tmp_path):
    ring, keys = _ring(tmp_path)
    app = MonitorApp(ctx, keys=ring, max_concurrent_runs=2, max_body_bytes=4096, store_bundle=_bundle(),
                     settings_path=tmp_path / "monitor_settings.json")
    base, server = _serve(app)
    try:
        req = urllib.request.Request(base + "/api/evals", data=json.dumps({"test_type": "smoke"}).encode(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "X-API-Key": keys["tester"],
                                                             "X-Request-Id": "req-corr-000001"})
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            batch = json.loads(r.read())["batch_id"]
        events = [e for e in app.ctx.audit.list("monitor") if e["action"] == "suite.started"]
        assert events and events[-1]["detail"]["request_id"] == "req-corr-000001"
        assert events[-1]["entity_id"] == batch and events[-1]["actor"] == "tester"
        app.drain(timeout_s=20)
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_the_last_suite_quality_is_on_the_metrics_endpoint(ctx, tmp_path):
    import time as _t

    app = MonitorApp(ctx, api_key=KEY, max_concurrent_runs=2, max_body_bytes=4096, store_bundle=_bundle())
    base, server = _serve(app)
    try:
        assert b"voiceshop_last_suite_gate_passed" not in _get(base + "/metrics")[1]  # nothing has finished yet
        app.try_acquire()
        app.submit_store_suite("smoke", pace_ms=0)
        deadline = _t.monotonic() + 30
        while app.metrics.last_gate_passed is None and _t.monotonic() < deadline:
            _t.sleep(0.05)
        text = _get(base + "/metrics")[1].decode()
        assert "voiceshop_last_suite_gate_passed 1" in text
        assert "voiceshop_last_suite_attack_success_rate" in text and "voiceshop_last_suite_reply_p95_ms" in text
    finally:
        server.shutdown()
        server.server_close()
        app.close()
