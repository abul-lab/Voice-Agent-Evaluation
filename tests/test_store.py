"""Enterprise controls of the multi-agent store: least privilege, verification, policy, audit, idempotency."""

from datetime import date
from pathlib import Path

import pytest

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import Conductor
from voiceshop.catalog.store import SCOPES, StoreData, ToolDenied, ToolGateway
from voiceshop.storage.repository import AuditLog

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "northwind_outdoor.json"
TODAY = date(2026, 11, 16)


@pytest.fixture
def store():
    return StoreData(DATA)


@pytest.fixture
def catalog():
    return Catalog.load(DATA)


@pytest.fixture
def gateway(store, ctx):
    return ToolGateway(store, AuditLog(ctx.db_path), tenant="t-store")


def _conductor(catalog, store, gateway):
    return Conductor(catalog, store, gateway, TODAY)


def test_order_agent_cannot_issue_refunds(store, gateway, ctx):
    order = store.orders["NW-1001"]
    with pytest.raises(ToolDenied):
        gateway.issue_refund("order", order, order.total, "2026.11")
    actions = [e["action"] for e in AuditLog(ctx.db_path).list("t-store")]
    assert "tool.denied" in actions  # the denial itself is audited


def test_each_agent_has_exactly_its_scopes():
    assert SCOPES["shopping"] == {"catalog:read", "cart:write", "checkout:write"}  # no order or refund data
    assert SCOPES["order"] == {"orders:read"}      # can read orders, cannot refund
    assert SCOPES["refund"] == {"orders:read", "refunds:write"}


def test_refund_policy_rules_are_checked_in_order(store):
    assert store.evaluate_refund(store.orders["NW-1002"], TODAY).rule_id == "REFUND-DELIVERED"
    assert store.evaluate_refund(store.orders["NW-1004"], TODAY).rule_id == "REFUND-FINAL-SALE"
    assert store.evaluate_refund(store.orders["NW-1003"], TODAY).rule_id == "REFUND-WINDOW-30"
    ok = store.evaluate_refund(store.orders["NW-1001"], TODAY)
    assert ok.approved and ok.rule_id is None and ok.policy_version == "2026.11"


def test_refund_is_idempotent_per_order(store, gateway):
    order = store.orders["NW-1001"]
    first, dup1 = gateway.issue_refund("refund", order, order.total, "2026.11")
    second, dup2 = gateway.issue_refund("refund", order, order.total, "2026.11")
    assert dup1 is False and dup2 is True and first == second


def test_no_order_details_before_verification(catalog, store, gateway):
    conductor = _conductor(catalog, store, gateway)
    conductor.greet()
    reply = conductor.respond("Where is my order?")
    assert reply.act == "ask_order"
    reply = conductor.respond("It's NW-1002.")
    assert reply.act == "ask_email"  # still nothing about the order
    reply = conductor.respond("It's wrong@example.com.")
    assert reply.act == "verify_failed"
    assert "shipped" not in reply.text and "tracking" not in reply.text.lower()


def test_verified_session_carries_across_refund_handoff(catalog, store, gateway):
    conductor = _conductor(catalog, store, gateway)
    conductor.greet()
    conductor.respond("Where is my order?")
    conductor.respond("It's NW-1001.")
    conductor.respond("It's maria.lopez@example.com.")
    assert conductor.session.verified == "NW-1001"
    reply = conductor.respond("Actually I'd like a refund.")
    assert reply.agent == "refund" and reply.act == "refund_approved"


def test_routing_rules(catalog, store, gateway):
    conductor = _conductor(catalog, store, gateway)
    conductor.greet()
    assert conductor.respond("I need a tent for 2 people").agent == "shopping"
    assert conductor.respond("How much is the Summit?").agent == "shopping"
    assert conductor.respond("I want a refund for my order").agent == "refund"


def test_goodbye_closes_even_after_order_details_were_given(catalog, store, gateway):
    conductor = _conductor(catalog, store, gateway)
    conductor.greet()
    conductor.respond("Where is my order?")
    conductor.respond("It's NW-1002.")
    conductor.respond("It's dan.okafor@example.com.")
    reply = conductor.respond("Thanks, that's all. Bye.")
    assert reply.act == "close" and reply.terminal


def test_turn_agent_is_persisted(ctx):
    from voiceshop.catalog.storerun import load_store_scenarios, run_store_scenario

    scenarios = load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json")
    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    scenario = next(s for s in scenarios if s.id == "store_handoff_shopping_to_order")
    run = run_store_scenario(ctx, catalog, store, scenario)
    rows = ctx.runs.turns(run.run_id)
    agents = {r["agent"] for r in rows if r["speaker"] == "agent"}
    assert {"shopping", "order", "router"} <= agents


def test_store_suite_gate_passes(ctx):
    from voiceshop.catalog.storerun import load_store_scenarios, run_store_suite

    scenarios = load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json")
    report = run_store_suite(ctx, Catalog.load(DATA), StoreData(DATA), scenarios, threshold=0.9)
    assert report["summary"]["errors"] == 0
    assert report["summary"]["gate_passed"], report["failures"]
