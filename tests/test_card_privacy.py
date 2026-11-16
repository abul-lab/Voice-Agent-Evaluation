"""S.1: a full card number is never spoken by the agent and never stored in a transcript."""

from pathlib import Path

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData
from voiceshop.catalog.storerun import load_store_scenarios, run_store_suite
from voiceshop.privacy import CARD_REMOVED, find_card_numbers, mask_card_numbers
from voiceshop.simulation.conversation import TurnRecord

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog" / "outdoor_store.json"
TEST_PAN = "4111 1111 1111 1111"  # a published test number that passes the Luhn check


def test_a_card_number_with_spaces_or_dashes_is_found_and_masked():
    assert find_card_numbers(f"my card is {TEST_PAN}") == ["4111111111111111"]
    assert mask_card_numbers("card 4111-1111-1111-1111 thanks") == f"card {CARD_REMOVED} thanks"


def test_order_numbers_phone_numbers_and_the_last_four_are_not_masked():
    text = "order NW-1001, phone 555 123 4567, test card ending 4242"
    assert find_card_numbers(text) == []
    assert mask_card_numbers(text) == text


def test_a_long_number_that_fails_the_luhn_check_is_not_treated_as_a_card():
    assert find_card_numbers("reference 4111111111111112") == []


def test_no_agent_reply_in_the_full_regression_contains_a_card_number(ctx):
    # The full regression tier: barge-in cases are a separate test type and are not part of it.
    scenarios = [s for s in load_store_scenarios(ROOT / "scenarios" / "store_scenarios.json")
                 if not any(t.startswith("barge_in") for t in s.tags)]
    report = run_store_suite(ctx, Catalog.load(DATA), StoreData(DATA), scenarios, threshold=0.9,
                             shops=ShopSessions())
    leaks = []
    for scenario_id, conversation in report["conversations"].items():
        for turn in conversation:
            if find_card_numbers(turn["text_spoken"]):
                leaks.append((scenario_id, turn["text_spoken"]))
    assert len(report["conversations"]) == len(scenarios)  # every case was actually spoken through
    assert leaks == []


def test_a_transcript_stores_a_card_number_only_in_masked_form(ctx):
    run_id = "card-privacy-1"
    ctx.runs.create(run_id, "card", "rules", "store", None)
    turn = TurnRecord(seq=1, speaker="customer", language="en", text_spoken=f"my card is {TEST_PAN}",
                      text_heard=f"my card is {TEST_PAN}", act=None, intent="say", latency_ms=None,
                      created_at="2026-11-16T10:00:00")
    ctx.runs.append_turn(run_id, turn)
    stored = ctx.runs.turns(run_id)[0]
    assert TEST_PAN.replace(" ", "") not in stored["text_heard"]
    assert CARD_REMOVED in stored["text_heard"]
    assert CARD_REMOVED in stored["text_spoken"]
