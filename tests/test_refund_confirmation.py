"""3.1: a refund is approved only after the agent has read it back and the caller has said yes."""

from datetime import date
from pathlib import Path

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.storage.repository import AuditLog, RefundLedger

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "outdoor_store.json"
EMAIL = "maria.lopez@example.com"


def _router(ctx, sid="refund-confirm"):
    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    shops = ShopSessions()
    gw = ToolGateway(store, AuditLog(ctx.db_path), tenant=sid, ledger=RefundLedger(ctx.db_path),
                     catalog=catalog, shops=shops)
    router = FrontDeskRouter(catalog, store, gw, date(2026, 11, 16), sessions=shops, sid=sid)
    return router, gw


def _approvals(ctx, sid="refund-confirm"):
    return [e for e in AuditLog(ctx.db_path).list(sid) if e["action"] == "refund.approved"]


def _ask_for_refund(router):
    router.respond("I want a refund")
    router.respond("order NW-1001")
    return router.respond(f"my email is {EMAIL}")


def test_the_read_back_asks_for_a_yes_and_approves_nothing(ctx):
    router, _ = _router(ctx)
    reply = _ask_for_refund(router)
    assert reply.act == "confirm_refund"
    assert "Shall I approve it" in reply.text
    assert _approvals(ctx) == []


def test_a_yes_after_the_read_back_approves_the_refund_once(ctx):
    router, _ = _router(ctx)
    _ask_for_refund(router)
    reply = router.respond("yes please")
    assert reply.act == "refund_approved"
    assert len(_approvals(ctx)) == 1


def test_anything_other_than_yes_asks_again_and_approves_nothing(ctx):
    router, _ = _router(ctx)
    _ask_for_refund(router)
    for words in ("hmm let me think about it", "what does that mean", "the weather is nice"):
        reply = router.respond(words)
        assert reply.act == "confirm_refund"
        assert "yes to approve" in reply.text and "no to cancel" in reply.text
    assert _approvals(ctx) == []


def test_a_no_cancels_the_refund_and_records_the_decline(ctx):
    router, _ = _router(ctx)
    _ask_for_refund(router)
    reply = router.respond("no, not yet")
    assert reply.act == "refund_declined"
    assert _approvals(ctx) == []
    actions = [e["action"] for e in AuditLog(ctx.db_path).list("refund-confirm")]
    assert "refund.declined" in actions


def test_while_waiting_for_a_yes_the_refund_agent_keeps_the_turn(ctx):
    router, _ = _router(ctx)
    _ask_for_refund(router)
    reply = router.respond("track my order")  # an order cue must not take over the read-back
    assert reply.agent == "refund"
    assert reply.act == "confirm_refund"
    assert _approvals(ctx) == []


def test_a_repeat_refund_after_approval_is_a_duplicate_with_no_new_question(ctx):
    router, _ = _router(ctx)
    _ask_for_refund(router)
    router.respond("yes")
    reply = router.respond("I'd like a refund for order NW-1001 again")
    assert reply.act == "refund_approved"
    assert "already approved" in reply.text
    assert len(_approvals(ctx)) == 1


def test_a_changed_order_number_drops_the_read_back(ctx):
    router, _ = _router(ctx)
    _ask_for_refund(router)
    assert router.session.pending_refund is not None
    router.respond("actually it's order NW-1002")
    assert router.session.pending_refund is None
    assert _approvals(ctx) == []
