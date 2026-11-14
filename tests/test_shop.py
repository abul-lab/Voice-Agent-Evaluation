"""Eliza Voice Shopping: storefront rules and the storefront HTTP endpoints."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from voiceshop.catalog import shop as sh
from voiceshop.catalog.model import Catalog
from voiceshop.catalog.shop import ShopError, ShopSessions
from voiceshop.catalog.store import StoreData
from voiceshop.catalog.storerun import load_store_scenarios
from voiceshop.monitor.server import MonitorApp, make_server

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "northwind_outdoor.json"


@pytest.fixture
def catalog():
    return Catalog.load(DATA)


@pytest.fixture
def shops():
    return ShopSessions()


def _mutate(shops, catalog, sid, fn):
    return shops.mutate(sid, catalog, fn, actor="test", action="test")


def test_order_cannot_be_placed_without_confirmation(shops, catalog):
    _mutate(shops, catalog, "s1", lambda s: sh.op_add(s, catalog, "tent-01"))
    with pytest.raises(ShopError) as exc:
        _mutate(shops, catalog, "s1", lambda s: sh.op_place_order(s, catalog))
    assert exc.value.code == "not_confirmed"


def test_confirmed_order_totals_the_cart_and_empties_it(shops, catalog):
    _mutate(shops, catalog, "s2", lambda s: sh.op_add(s, catalog, "tent-01"))
    _mutate(shops, catalog, "s2", lambda s: sh.op_add(s, catalog, "stove-01"))
    _mutate(shops, catalog, "s2", lambda s: sh.op_request_order(s, catalog))
    order = _mutate(shops, catalog, "s2", lambda s: sh.op_place_order(s, catalog))
    assert order["total"] == 189 + 59
    snap = shops.snapshot("s2", catalog)
    assert snap["cart"] == [] and snap["page"].startswith("confirmation:") and len(snap["orders"]) == 1


def test_out_of_stock_items_cannot_be_added(shops, catalog):
    with pytest.raises(ShopError) as exc:
        _mutate(shops, catalog, "s3", lambda s: sh.op_add(s, catalog, "tent-03"))
    assert exc.value.code == "out_of_stock"
    assert shops.snapshot("s3", catalog)["cart"] == []


def test_checkout_needs_a_non_empty_cart(shops, catalog):
    with pytest.raises(ShopError) as exc:
        _mutate(shops, catalog, "s4", lambda s: sh.op_request_order(s, catalog))
    assert exc.value.code == "cart_empty"


def test_cart_survives_a_switch_to_clicks_and_back(shops, catalog):
    _mutate(shops, catalog, "s5", lambda s: sh.op_add(s, catalog, "bag-03"))
    _mutate(shops, catalog, "s5", lambda s: sh.op_set_mode(s, "interactive"))
    _mutate(shops, catalog, "s5", lambda s: sh.op_set_mode(s, "voice"))
    snap = shops.snapshot("s5", catalog)
    assert snap["mode"] == "voice" and snap["cart_count"] == 1


def test_every_change_is_versioned_and_logged(shops, catalog):
    v0 = shops.snapshot("s6", catalog)["version"]
    _mutate(shops, catalog, "s6", lambda s: sh.op_add(s, catalog, "pack-01"))
    snap = shops.snapshot("s6", catalog)
    assert snap["version"] == v0 + 1
    assert snap["events"][-1]["action"] == "test"


@pytest.fixture
def live_shop(settings, ctx, catalog):
    scenarios = load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json")
    bundle = {"catalog": catalog, "store": StoreData(DATA), "scenarios": scenarios, "shops": ShopSessions()}
    app = MonitorApp(ctx, api_key=None, max_concurrent_runs=1, max_body_bytes=4096, store_bundle=bundle)
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    app.close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 loopback test server
        return r.status, json.loads(r.read())


def _act(base, body):
    req = urllib.request.Request(base + "/shop/api/action", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_storefront_pages_are_served(live_shop):
    with urllib.request.urlopen(live_shop + "/shop", timeout=5) as r:  # noqa: S310
        body = r.read()
    assert r.status == 200 and b"Eliza Voice Shopping" in body and b"/shop.js" in body
    status, cat = _get(live_shop + "/shop/api/catalog")
    assert status == 200 and cat["store"] == "Eliza Voice Shopping" and "tent-01" in cat["products"]


def test_shopper_clicks_follow_the_same_rules(live_shop):
    sid = "test-session-1"
    status, snap = _act(live_shop, {"sid": sid, "action": "add", "product_id": "tent-03"})
    assert status == 409 and snap["error"]["code"] == "out_of_stock"
    status, snap = _act(live_shop, {"sid": sid, "action": "add", "product_id": "tent-01"})
    assert status == 200 and snap["cart_count"] == 1
    status, snap = _act(live_shop, {"sid": sid, "action": "place_order"})
    assert status == 200 and snap["orders"] and snap["cart"] == []


def test_storefront_rejects_unknown_actions_and_pages(live_shop):
    assert _act(live_shop, {"sid": "x-1", "action": "delete_everything"})[0] == 400
    assert _act(live_shop, {"sid": "x-1", "action": "navigate", "page": "../../etc"})[0] == 400
    assert _act(live_shop, {"sid": "bad sid!", "action": "checkout"})[0] == 400


def test_takeover_and_resume_over_http(live_shop):
    sid = "test-session-2"
    _act(live_shop, {"sid": sid, "action": "takeover"})
    _, snap = _get(f"{live_shop}/shop/api/state?sid={sid}")
    assert snap["mode"] == "interactive"
    _act(live_shop, {"sid": sid, "action": "resume"})
    _, snap = _get(f"{live_shop}/shop/api/state?sid={sid}")
    assert snap["mode"] == "voice"
