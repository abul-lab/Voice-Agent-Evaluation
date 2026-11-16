"""3.2: a caller the agent does not understand gets asked again, then the options, then a person."""

from datetime import date
from pathlib import Path

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.storage.repository import AuditLog, RefundLedger

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "outdoor_store.json"


def _router(ctx, sid="repair"):
    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    shops = ShopSessions()
    gw = ToolGateway(store, AuditLog(ctx.db_path), tenant=sid, ledger=RefundLedger(ctx.db_path),
                     catalog=catalog, shops=shops)
    return FrontDeskRouter(catalog, store, gw, date(2026, 11, 16), sessions=shops, sid=sid)


def test_three_misses_climb_the_ladder_and_hand_over(ctx):
    router = _router(ctx)
    acts = [router.respond(w).act for w in ("purple monkey", "zorp flib", "qwerty plonk")]
    assert acts == ["out_of_scope", "offer_options", "recovery_handoff"]


def test_the_handover_gives_the_reason_code(ctx):
    router = _router(ctx)
    for w in ("purple monkey", "zorp flib"):
        router.respond(w)
    reply = router.respond("qwerty plonk")
    assert "REPAIR_LIMIT" in reply.text
    assert reply.params == {"code": "REPAIR_LIMIT"}


def test_an_understood_turn_restarts_the_count(ctx):
    router = _router(ctx)
    router.respond("purple monkey")
    router.respond("I need a tent for two people")
    acts = [router.respond(w).act for w in ("zorp flib", "qwerty plonk")]
    assert acts == ["out_of_scope", "offer_options"]


def test_a_correction_asks_again_rather_than_acting_on_the_last_answer(ctx):
    router = _router(ctx)
    router.respond("I need a tent for two people")
    reply = router.respond("that's not what I asked")
    assert reply.act == "clarify"
    assert reply.agent == "router"


def test_corrections_climb_the_same_ladder(ctx):
    router = _router(ctx)
    router.respond("I need a tent for two people")
    acts = [router.respond("that's not what I asked").act for _ in range(3)]
    assert acts == ["clarify", "offer_options", "recovery_handoff"]


def test_the_greeting_is_not_a_miss(ctx):
    router = _router(ctx)
    router.respond("hello")
    assert router.session.misses == 0
