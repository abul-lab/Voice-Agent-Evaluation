"""S.2: transcripts and the audit trail need a key to read. The storefront stays open."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from voiceshop.monitor.auth import KeyRing, Principal, TicketStore, hash_key
from voiceshop.monitor.server import MonitorApp, make_server

VIEWER_KEY = "k-viewer-000000001"


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def run_id(ctx):
    rid = "read-access-1"
    ctx.runs.create(rid, "smoke", "rules", "store", None)
    ctx.runs.finish(rid, "passed", {"passed": 1})
    return rid


def _bundle():
    from voiceshop.catalog.model import Catalog
    from voiceshop.catalog.shop import ShopSessions
    from voiceshop.catalog.store import StoreData

    data = Path(__file__).resolve().parents[1] / "data" / "catalog" / "northwind_outdoor.json"
    return {"catalog": Catalog.load(data), "store": StoreData(data), "scenarios": [], "shops": ShopSessions()}


def _serve(app):
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


def _status(url):
    """Status code only: for SSE streams, whose body is not JSON."""
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - loopback test server
            r.read(64)
            return r.status
    except urllib.error.HTTPError as err:
        return err.code


def _request(url, *, key=None, method="GET", body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - loopback test server
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


@pytest.fixture
def keyed(ctx, run_id):
    ring = KeyRing([{"name": "reader", "role": "viewer", "sha256": hash_key(VIEWER_KEY)}])
    app = MonitorApp(ctx, keys=ring, max_concurrent_runs=1, max_body_bytes=4096, store_bundle=_bundle())
    base, server = _serve(app)
    yield base, app
    server.shutdown()
    app.close()


def test_run_transcripts_and_audit_need_a_key(keyed, run_id):
    base, _ = keyed
    for path in ("/api/runs", f"/api/runs/{run_id}", f"/api/runs/{run_id}/turns", f"/api/runs/{run_id}/audit"):
        status, body = _request(base + path)
        assert status == 401, path
        assert body["error"]["code"] == "unauthorised"


def test_a_valid_key_reads_transcripts(keyed, run_id):
    base, _ = keyed
    assert _request(base + "/api/runs", key=VIEWER_KEY)[0] == 200
    assert _request(base + f"/api/runs/{run_id}/audit", key=VIEWER_KEY)[0] == 200


def test_a_wrong_key_is_refused(keyed):
    base, _ = keyed
    assert _request(base + "/api/runs", key="not-a-key-at-all-1")[0] == 401


def test_the_storefront_stays_open_without_a_key(keyed):
    base, _ = keyed
    assert _request(base + "/shop/api/catalog")[0] == 200


def test_without_any_key_configured_run_records_are_refused(ctx, run_id):
    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=4096)
    base, server = _serve(app)
    try:
        status, body = _request(base + "/api/runs")
        assert status == 403 and body["error"]["code"] == "api_disabled"
    finally:
        server.shutdown()
        app.close()


def test_the_local_dashboard_exception_still_reads_without_a_key(ctx, run_id):
    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=4096, local_ui_no_key=True)
    base, server = _serve(app)
    try:
        assert _request(base + "/api/runs")[0] == 200
    finally:
        server.shutdown()
        app.close()


def test_a_stream_ticket_opens_one_stream_and_expires(keyed, run_id):
    base, app = keyed
    status, body = _request(base + "/api/stream-tickets", key=VIEWER_KEY, method="POST")
    assert status == 200 and body["expires_in_s"] == 60
    ticket = body["ticket"]
    # EventSource cannot send the header, so the ticket goes in the query string.
    assert _status(base + f"/api/runs/{run_id}/stream?ticket={ticket}") == 200
    assert _status(base + f"/api/runs/{run_id}/stream?ticket={ticket}") == 401  # single use


def test_a_stream_ticket_needs_a_key_to_be_issued(keyed):
    base, _ = keyed
    assert _request(base + "/api/stream-tickets", method="POST")[0] == 401


def test_a_ticket_cannot_open_the_storefront_or_other_routes(keyed):
    base, _ = keyed
    ticket = _request(base + "/api/stream-tickets", key=VIEWER_KEY, method="POST")[1]["ticket"]
    assert _request(base + f"/api/runs?ticket={ticket}")[0] == 401


def test_tickets_expire_after_their_lifetime():
    clock = _Clock()
    store = TicketStore(ttl_s=60, clock=clock)
    token = store.issue(Principal("reader", "viewer"))
    clock.now += 61
    assert store.redeem(token) is None


def test_a_ticket_is_redeemed_only_once():
    store = TicketStore(ttl_s=60, clock=_Clock())
    token = store.issue(Principal("reader", "viewer"))
    assert store.redeem(token).name == "reader"
    assert store.redeem(token) is None
    assert store.redeem(None) is None
