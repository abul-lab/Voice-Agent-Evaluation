"""3.3: caller speech over the agent stops playback when barge-in is on; otherwise the reply finishes."""

from datetime import date
from pathlib import Path

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.config import describe, load_settings
from voiceshop.evaluation.conversation_metrics import conversation_metrics
from voiceshop.speech.barge_in import STOP_BUDGET_MS, BargeIn, Playback
from voiceshop.storage.repository import AuditLog, RefundLedger

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "outdoor_store.json"
REPLY = "Our four-person tent weighs 2.1 kilograms and sleeps four comfortably in the valley."


def _playback():
    return Playback(text=REPLY, start_ms=1000)


def test_with_barge_in_off_the_reply_always_finishes():
    assert BargeIn(enabled=False).check(_playback(), caller_started_ms=1500) is None


def test_caller_speech_during_the_reply_stops_it_within_budget():
    p = _playback()
    cut = BargeIn(enabled=True).check(p, caller_started_ms=p.start_ms + 1500)
    assert cut is not None
    assert cut.stop_latency_ms == 150
    assert cut.within_budget and cut.stop_latency_ms <= STOP_BUDGET_MS


def test_the_caller_hears_only_the_words_said_before_the_stop():
    p = _playback()
    cut = BargeIn(enabled=True).check(p, caller_started_ms=p.start_ms + 1500)
    # 1650 ms into the reply at 380 ms per word: four words have been spoken.
    assert cut.heard == "Our four-person tent weighs"
    assert cut.words_heard == 4 and cut.words_total == len(REPLY.split())


def test_speech_before_or_after_the_reply_is_not_an_interruption():
    p = _playback()
    b = BargeIn(enabled=True)
    assert b.check(p, caller_started_ms=p.start_ms - 1) is None
    assert b.check(p, caller_started_ms=p.end_ms + 10) is None


def test_speech_in_the_last_moments_is_not_an_interruption_when_the_reply_ends_first():
    p = _playback()
    b = BargeIn(enabled=True)
    assert b.check(p, caller_started_ms=p.end_ms - 50) is None


def test_a_slow_channel_misses_the_stop_budget():
    p = _playback()
    cut = BargeIn(enabled=True, detection_ms=400).check(p, caller_started_ms=p.start_ms + 1500)
    assert cut is not None and not cut.within_budget


def test_the_flag_comes_from_the_environment_and_is_off_by_default():
    assert load_settings({}).barge_in is False
    assert load_settings({"VOICESHOP_BARGE_IN": "1"}).barge_in is True
    assert describe(load_settings({}))["barge_in"] is False


def test_the_router_takes_the_callers_words_after_an_interruption(ctx):
    catalog = Catalog.load(DATA)
    store = StoreData(DATA)
    shops = ShopSessions()
    gw = ToolGateway(store, AuditLog(ctx.db_path), tenant="barge", ledger=RefundLedger(ctx.db_path),
                     catalog=catalog, shops=shops)
    router = FrontDeskRouter(catalog, store, gw, date(2026, 11, 16), sessions=shops, sid="barge")
    first = router.respond("I need a tent for two people")
    p = Playback(text=first.text, start_ms=0)
    assert BargeIn(enabled=True).check(p, caller_started_ms=1200) is not None
    # The caller's interrupting words are the next turn, handled like any other utterance.
    second = router.respond("Actually, what about the price of the valley one?")
    assert second.agent in ("shopping", "router")


def test_interrupted_turns_are_counted_when_a_channel_records_them():
    convo = [
        {"speaker": "agent", "act": "recommend", "interrupted": True, "stop_latency_ms": 150,
         "within_budget": True, "text_spoken": "Our four-person tent"},
        {"speaker": "agent", "act": "recommend", "interrupted": False, "text_spoken": "Done."},
    ]
    out = conversation_metrics([{"scenario_id": "a", "passed": True, "checks": [], "conversation": convo}])
    b = out["interruptions"]
    assert b["measurable"] is True
    assert b["count"] == 1 and b["interruption_rate"] == 0.5
    assert b["within_stop_budget_rate"] == 1.0
