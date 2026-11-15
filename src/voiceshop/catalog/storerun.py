"""Store scenarios: loading, execution, evaluation and the suite report.

Every check reads the transcript, the database-backed audit log and the store data. None of them trust
the agent's own claims.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time as _clock
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from voiceshop.catalog.model import Catalog, Product
from voiceshop.catalog.multiagent import FrontDeskRouter
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.shopper import GOALS, Goal, ShopperSimulator
from voiceshop.catalog.store import StoreData, ToolGateway
from voiceshop.errors import ScenarioError
from voiceshop.evaluation.analysis import analyse, category_of
from voiceshop.evaluation.checks import Check
from voiceshop.evaluation.harness import aggregate, write_reports
from voiceshop.evaluation.invariants import dialogue_violations
from voiceshop.simulation.asr import AsrNoise
from voiceshop.simulation.conversation import TurnRecord, run_conversation
from voiceshop.speech.barge_in_turns import BargeInAgent, BargeInCaller, BargeInSpec, clips_dir_from_env

log = logging.getLogger("voiceshop.storerun")
DISCLOSING = {"order_status", "refund_approved", "refund_refused"}
REPAIR_ACTS = {"out_of_scope", "clarify", "offer_options", "recovery_handoff"}
VERIFICATION_ACTS = {"ask_order", "ask_email", "verify_failed"}
SEASON_TEXT = {"winter": 4, "summer": 2, "three season": 3, "spring": 3, "autumn": 3}


@dataclass(frozen=True)
class StoreScenario:
    id: str
    description: str
    tags: tuple[str, ...]
    seed: int
    today: date
    max_turns: int
    asr_error_rate: float
    faults: dict[str, int]
    off_topic_first: bool
    goals: tuple[Goal, ...]
    expect: dict[str, Any]
    ambient: str | None = None  # background sound the speech recogniser also picks up, e.g. "car passing by"
    barge_in: dict[str, Any] | None = None  # interrupt one agent reply with a caller clip (see barge_in_turns)
    extra: dict[str, Any] = field(default_factory=dict)


def _barge_in(raw: dict[str, Any] | None, sid: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        BargeInSpec.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ScenarioError(f"{sid}: barge_in needs turn, interrupt_after_words, clip and min_speech_ms ({exc})") from exc
    return dict(raw)


def _goal(raw: dict[str, Any], sid: str) -> Goal:
    if raw.get("intent") not in GOALS:
        raise ScenarioError(f"{sid}: unknown goal intent {raw.get('intent')!r}")
    g = Goal(**raw)
    if g.intent in ("order", "refund") and not (g.order_no and g.email):
        raise ScenarioError(f"{sid}: {g.intent} goals need order_no and email")
    if g.intent in ("price", "stock") and not g.product_text:
        raise ScenarioError(f"{sid}: {g.intent} goals need product_text")
    if g.intent == "compare" and not (g.product_text and g.product_text2):
        raise ScenarioError(f"{sid}: compare goals need two product texts")
    return g


def load_store_scenarios(path: Path) -> list[StoreScenario]:
    if not path.is_file():
        raise ScenarioError(f"scenario file not found: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1:
        raise ScenarioError(f"{path.name}: schema_version must be 1")
    out: list[StoreScenario] = []
    for raw in doc["scenarios"]:
        sid = raw["id"]
        out.append(StoreScenario(
            id=sid,
            description=raw.get("description", ""),
            tags=tuple(raw.get("tags", [])),
            seed=int(raw.get("seed", 1)),
            today=date.fromisoformat(raw["today"]),
            max_turns=int(raw.get("max_turns", 16)),
            asr_error_rate=float(raw.get("asr_error_rate", 0.0)),
            faults=dict(raw.get("faults", {})),
            off_topic_first=bool(raw.get("off_topic_first", False)),
            goals=tuple(_goal(g, sid) for g in raw["goals"]),
            expect=dict(raw.get("expect", {})),
            ambient=raw.get("ambient"),
            barge_in=_barge_in(raw.get("barge_in"), sid),
        ))
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise ScenarioError("duplicate store scenario ids")
    return out


@dataclass
class StoreRun:
    run_id: str
    scenario: StoreScenario
    turns: list[TurnRecord]
    end_reason: str | None
    error: str | None
    wall_ms: float
    shop: dict[str, Any] = field(default_factory=dict)  # final storefront state for this session
    barge_in: dict[str, Any] | None = None  # the barge-in outcome, when the scenario has one
    cancelled: bool = False  # the operator stopped the suite during this run; this is not an agent error


class AmbientAsr:
    """Wraps the speech model: background sound shows up in the transcript as a bracketed tag, the way a
    recogniser reports it. The agent has to ignore it and still understand what the caller said."""

    def __init__(self, inner: AsrNoise, ambient: str) -> None:
        self.inner = inner
        self.ambient = ambient

    def transform(self, spoken: str, language: str) -> str:
        return f"{self.inner.transform(spoken, language)} [{self.ambient}]"


def run_store_scenario(ctx, catalog: Catalog, store: StoreData, scenario: StoreScenario, *, source: str = "store",
                       mode: str = "rules", classifier=None,
                       batch_id: str | None = None, on_turn=None, shops: ShopSessions | None = None) -> StoreRun:
    run_id = f"{scenario.id}-{uuid.uuid4().hex[:8]}"
    ctx.runs.create(run_id, scenario.id, mode, source, batch_id)
    shops = shops or ShopSessions()
    # The storefront session is keyed by the run id, so the page shows exactly this conversation's cart.
    gateway = ToolGateway(store, ctx.audit, tenant=run_id, ledger=ctx.refunds, faults=scenario.faults,
                          catalog=catalog, shops=shops)
    conductor = FrontDeskRouter(catalog, store, gateway, scenario.today, sessions=shops, sid=run_id,
                                classifier=classifier)
    rng = random.Random(scenario.seed)
    caller = ShopperSimulator(list(scenario.goals), rng, scenario.off_topic_first)
    barge = None  # the agent wrapper, when this scenario interrupts a reply
    asr = AsrNoise(scenario.asr_error_rate, rng)
    if scenario.ambient:
        asr = AmbientAsr(asr, scenario.ambient)
    turns: list[TurnRecord] = []

    def emit(record: TurnRecord) -> None:
        record.shop_version = shops.session(run_id).version
        turns.append(record)
        ctx.runs.append_turn(run_id, record)
        if on_turn:
            on_turn(record)

    started = _clock.perf_counter()
    end_reason = error = None
    cancelled = False
    try:
        agent_side, caller_side = conductor, caller
        if scenario.barge_in is not None:
            barge = BargeInAgent(conductor, BargeInSpec.from_dict(scenario.barge_in), clips_dir_from_env())
            agent_side, caller_side = barge, BargeInCaller(caller, barge)
        result = run_conversation(agent_side, caller_side, asr, max_turns=scenario.max_turns, on_turn=emit)
        end_reason = result.end_reason
    except Exception as exc:  # isolation boundary: the run is recorded as an error, the suite carries on
        if getattr(exc, "code", None) == "cancelled":
            cancelled = True  # stopped by the operator: recorded as stopped, not as an agent error
        else:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("scenario failed", extra={"run_id": run_id, "scenario_id": scenario.id})
    wall = round((_clock.perf_counter() - started) * 1000, 1)
    snap = shops.snapshot(run_id, catalog)
    shop_state = {"orders": snap["orders"], "cart": {line["id"]: line["qty"] for line in snap["cart"]},
                  "mode": snap["mode"], "page": snap["page"]}
    outcome = barge.outcome.to_dict() if barge is not None and barge.outcome is not None else None
    return StoreRun(run_id, scenario, turns, end_reason, error, wall, shop_state, barge_in=outcome,
                    cancelled=cancelled)


def _path(turns: list[TurnRecord]) -> list[str]:
    agents = [t.agent for t in turns if t.speaker == "agent" and t.agent and t.agent != "router"]
    out: list[str] = []
    for a in agents:
        if not out or out[-1] != a:
            out.append(a)
    return out


def _barge_in_checks(run: StoreRun, agent_turns: list[TurnRecord]) -> list[Check]:
    """Checks for a scenario that interrupts a reply. ``agent_turns[0]`` is the greeting, so reply n is
    ``agent_turns[n]``."""
    b = run.barge_in or {}
    if not b.get("valid"):
        return [Check("barge_in_timing_valid", False, b.get("invalid_reason") or "the interruption did not happen")]
    checks = [Check("barge_in_timing_valid", True,
                    f"caller spoke {b['caller_start_ms']:.0f} ms into a {b['words_total']}-word reply")]
    expect_stop = b["expect"] == "stop"
    did_stop = bool(b["cut"])
    checks.append(Check("barge_in_decision", did_stop == expect_stop,
                        f"{b['kind']} clip: agent {'stopped' if did_stop else 'kept talking'}, "
                        f"expected {'stop' if expect_stop else 'continue'}"))
    if did_stop:
        after = agent_turns[b["turn"] + 1] if len(agent_turns) > b["turn"] + 1 else None
        answered = after is not None and after.act != "error" and after.text_spoken != b["reply_text"]
        checks.append(Check("barge_in_answered", answered,
                            "the agent answered the interruption" if answered else "no new answer after the cut"))
    return checks


def evaluate_store_run(catalog: Catalog, store: StoreData, run: StoreRun, audit_entries: list[dict[str, Any]]):
    sc = run.scenario
    expect = sc.expect
    turns = run.turns
    agent_turns = [t for t in turns if t.speaker == "agent"]
    checks: list[Check] = []

    if run.error:
        checks.append(Check("conversation_completed", False, run.error))
        return checks, {"error": run.error, "wall_ms": run.wall_ms}

    checks.append(Check("conversation_completed", run.end_reason in ("terminal_act", "customer_done"),
                        f"end_reason={run.end_reason}"))

    # routing: which specialists handled the call, in order
    path = _path(turns)
    if "agent_path" in expect:
        checks.append(Check("routing_correct", path == expect["agent_path"],
                            f"expected {expect['agent_path']}, got {path}"))

    # verification gate: order details are only disclosed after the caller's email matches the order
    gate_ok, gate_detail = True, "no order details disclosed before verification"
    for i, t in enumerate(turns):
        if t.speaker != "agent" or t.act not in DISCLOSING:
            continue
        m = re.search(r"NW-\d{4}", t.text_spoken)
        order_no = m.group(0) if m else None
        email = store.orders[order_no].email if order_no in store.orders else None
        heard = " ".join(x.text_heard.lower() for x in turns[:i] if x.speaker == "customer")
        if not email or email not in heard:
            gate_ok = False
            gate_detail = f"turn {t.seq} disclosed {order_no} before the caller verified"
            break
    checks.append(Check("verification_gate", gate_ok, gate_detail))

    if expect.get("no_repeat_verification"):
        first = next((i for i, t in enumerate(turns) if t.speaker == "agent" and t.act in DISCLOSING), None)
        again = [t.act for t in turns[first:] if t.speaker == "agent" and t.act in VERIFICATION_ACTS] if first is not None else []
        checks.append(Check("no_repeat_verification", not again, f"verification repeated: {again}" if again else
                            "verified once and carried across specialists"))

    # shopping answers
    named = lambda text: [p for p in catalog.products.values() if p.name in text]  # noqa: E731
    if expect.get("recommend_any_of"):
        ids = {p.id for t in agent_turns if t.act == "recommend" for p in named(t.text_spoken)}
        checks.append(Check("task_completed", bool(ids & set(expect["recommend_any_of"])),
                            f"recommended {sorted(ids) or 'nothing'}"))
    if expect.get("price"):
        w = expect["price"]
        hit = any(t.act == "answer_price" and catalog.get(w["product"]).name in t.text_spoken
                  and f"${w['value']}" in t.text_spoken for t in agent_turns)
        checks.append(Check("task_completed", hit, f"expected ${w['value']} for {w['product']}"))
    if expect.get("stock"):
        w = expect["stock"]
        name = catalog.get(w["product"]).name
        claim = "is in stock" if w["in_stock"] else "out of stock"
        hit = any(t.act == "answer_stock" and name in t.text_spoken and claim in t.text_spoken for t in agent_turns)
        checks.append(Check("task_completed", hit, f"expected {name} {claim}"))
    if expect.get("compare_pair"):
        a, b = (catalog.get(x).name for x in expect["compare_pair"])
        hit = any(t.act == "compare" and a in t.text_spoken and b in t.text_spoken for t in agent_turns)
        checks.append(Check("task_completed", hit, f"expected comparison of {a} and {b}"))

    # shopping recommendations must fit what the caller asked for
    if any(t.act == "recommend" for t in agent_turns):
        bad = []
        goal = next((g for g in sc.goals if g.intent == "recommend"), None)
        for t in agent_turns:
            if t.act != "recommend" or goal is None:
                continue
            for p in named(t.text_spoken):
                why = _misfit(catalog, goal, p)
                if why:
                    bad.append(f"{p.name}: {why}")
        checks.append(Check("recommendations_fit", not bad, "; ".join(bad) or "all recommendations fit the request"))

    if expect.get("act"):
        checks.append(Check("task_completed", expect["act"] in [t.act for t in agent_turns],
                            f"expected act {expect['act']}"))

    # policy decisions and grounded facts
    if expect.get("refund"):
        w = expect["refund"]
        acts = [t for t in agent_turns if t.act in ("refund_approved", "refund_refused")]
        last = acts[-1] if acts else None
        if w["decision"] == "approved":
            ok = last is not None and last.act == "refund_approved" and f"${store.orders[w['order']].total}" in last.text_spoken
            detail = f"expected approval of {w['order']}"
        else:
            ok = last is not None and last.act == "refund_refused" and w["rule_id"] in last.text_spoken
            detail = f"expected refusal citing {w['rule_id']}"
        checks.append(Check("refund_policy", ok, detail))

        logged = [e for e in audit_entries if e["entity_id"] == w["order"]]
        if w["decision"] == "approved":
            ok_audit = any(e["action"] == "refund.approved" for e in logged)
        else:
            ok_audit = any(e["action"] == "refund.refused" and e["detail"].get("rule_id") == w["rule_id"]
                           for e in logged)
        checks.append(Check("audit_trail", ok_audit, f"audit entries for {w['order']}: {[e['action'] for e in logged]}"))

    if expect.get("refund") and expect["refund"]["decision"] == "approved":
        # Approval is only valid after the read-back and the caller's yes: a confirm act must come first.
        first_approval = next((i for i, t in enumerate(agent_turns) if t.act == "refund_approved"), None)
        read_back = any(t.act == "confirm_refund" for t in agent_turns[:first_approval or 0])
        checks.append(Check("refund_confirmed_first", first_approval is not None and read_back,
                            "the refund was read back and confirmed before it was approved"))

    if expect.get("repair_ladder"):
        # Three misses in a row: ask again, offer the options, then hand over to a person. Nothing else in between.
        repairs = [t.act for t in agent_turns if t.act in REPAIR_ACTS]
        ok = repairs[:3] == ["out_of_scope", "offer_options", "recovery_handoff"]
        checks.append(Check("repair_ladder", ok, f"repair acts in order: {repairs}"))

    if expect.get("declined"):
        approvals = [e for e in audit_entries if e["action"] == "refund.approved"]
        declined = any(t.act == "refund_declined" for t in agent_turns)
        checks.append(Check("refund_declined_leaves_ledger_unchanged", declined and not approvals,
                            f"declined={declined}, approvals recorded={len(approvals)}"))

    if expect.get("idempotent"):
        approvals = [e for e in audit_entries if e["action"] == "refund.approved"]
        duplicates = [t for t in agent_turns if t.act == "refund_approved" and "already approved" in t.text_spoken]
        checks.append(Check("idempotent_refund", len(approvals) == 1 and len(duplicates) >= 1,
                            f"{len(approvals)} approval(s) recorded, {len(duplicates)} duplicate answer(s)"))

    if expect.get("fault"):
        faulted = any(t.act == "service_error" for t in agent_turns)
        resolved = any(t.act in DISCLOSING for t in agent_turns)
        leaked = any(t.act == "service_error" and "NW-" in t.text_spoken for t in agent_turns)
        checks.append(Check("graceful_failure", faulted and resolved and not leaked,
                            "timeout surfaced as a service message, then resolved" if faulted and resolved
                            else "timeout not handled gracefully"))

    if expect.get("order_status"):
        hit = any(t.act == "order_status" and expect["order_status"] in t.text_spoken for t in agent_turns)
        checks.append(Check("task_completed", hit, f"expected order status '{expect['order_status']}'"))

    if expect.get("escalation"):
        code = expect["escalation"]
        hit = any(t.act == "escalate" and code in t.text_spoken for t in agent_turns)
        checks.append(Check("escalation_code", hit, f"expected escalation {code}"))

    prices = {p.price for p in catalog.products.values()} | store.prices()
    bad_money = [m for t in agent_turns for m in re.findall(r"\$(\d+)", t.text_spoken) if int(m) not in prices]
    checks.append(Check("grounded_facts", not bad_money,
                        f"amounts not in catalogue or orders: {bad_money}" if bad_money else "all amounts match the data"))

    # storefront outcomes
    placed = run.shop.get("orders", [])
    if expect.get("order"):
        w = expect["order"]
        ok = (len(placed) == 1 and sorted(i["id"] for i in placed[0]["items"]) == sorted(w["items"])
              and placed[0]["total"] == w["total"])
        seen = [(o["id"], o["total"], [i["id"] for i in o["items"]]) for o in placed]
        checks.append(Check("commerce_task", ok, f"orders placed {seen}; expected items {w['items']} total {w['total']}"))
    if expect.get("no_order"):
        checks.append(Check("commerce_task", not placed,
                            "no order placed, as expected" if not placed else f"unexpected order {placed[0]['id']}"))
    if expect.get("cart_final") is not None:
        got = sorted(run.shop.get("cart", {}))
        want = sorted(expect["cart_final"])
        checks.append(Check("cart_consistency", got == want, f"cart at the end {got}, expected {want}"))
    if placed or any(t.act == "order_placed" for t in agent_turns):
        seq = [t.act for t in agent_turns]
        i_order = seq.index("order_placed") if "order_placed" in seq else -1
        confirmed = i_order > 0 and "confirm_order" in seq[:i_order]
        checks.append(Check("confirmation_before_order", confirmed,
                            "the shopper confirmed before the order was placed" if confirmed
                            else "an order was placed without a confirmation step"))
    # guardrails: things the agent must never do in this scenario
    if expect.get("forbidden_acts"):
        bad = [t.act for t in agent_turns if t.act in expect["forbidden_acts"]]
        checks.append(Check("no_forbidden_act", not bad, f"forbidden acts used: {bad}" if bad
                            else "no forbidden act was used"))
    if expect.get("forbidden_text"):
        words = [w.lower() for w in expect["forbidden_text"]]
        leaked = [(t.seq, w) for t in agent_turns for w in words if w in t.text_spoken.lower()]
        checks.append(Check("no_forbidden_text", not leaked, f"agent said forbidden text: {leaked}" if leaked
                            else "nothing forbidden was said"))
    if expect.get("forbidden_audit"):
        acts = [e["action"] for e in audit_entries if e["action"] in expect["forbidden_audit"]]
        checks.append(Check("no_forbidden_tool_call", not acts, f"audit recorded {acts}" if acts
                            else "no forbidden tool call was made"))

    if expect.get("acts"):
        seen_acts = {t.act for t in agent_turns}
        missing = [a for a in expect["acts"] if a not in seen_acts]
        checks.append(Check("task_completed", not missing,
                            f"missing steps {missing}" if missing else "every expected step happened"))

    budget = expect.get("max_turns", sc.max_turns)
    checks.append(Check("turn_budget", len(turns) <= budget, f"{len(turns)} turns (budget {budget})"))
    violations = dialogue_violations(turns)
    checks.append(Check("dialogue_invariants", not violations, "; ".join(violations) or "all conduct invariants held"))

    if run.scenario.barge_in is not None:
        if run.barge_in is None:
            checks.append(Check("barge_in_timing_valid", False, "the agent never produced the reply to interrupt"))
        else:
            checks.extend(_barge_in_checks(run, agent_turns))

    per_agent: dict[str, int] = {}
    for t in agent_turns:
        if t.agent:
            per_agent[t.agent] = per_agent.get(t.agent, 0) + 1
    metrics = {
        "turns": len(turns),
        "agent_latency_ms": [t.latency_ms for t in agent_turns if t.latency_ms is not None],
        "path": path,
        "turns_by_agent": per_agent,
        "wall_ms": run.wall_ms,
    }
    return checks, metrics


def _misfit(catalog: Catalog, goal: Goal, p: Product) -> str:
    problems = []
    if goal.category_text and p.category != _category(catalog, goal.category_text):
        problems.append("wrong category")
    if p.category == "tent" and goal.people and p.capacity < goal.people:
        problems.append(f"sleeps {p.capacity}, need {goal.people}")
    if goal.budget is not None and p.price > goal.budget:
        problems.append(f"${p.price} over budget ${goal.budget}")
    if goal.season_text and p.season < SEASON_TEXT.get(goal.season_text, 3):
        problems.append("not rated for the season")
    if goal.lightweight and not catalog.is_lightweight(p):
        problems.append(f"{p.weight_kg} kg is not lightweight")
    if p.stock <= 0:
        problems.append("out of stock")
    return "; ".join(problems)


def _category(catalog: Catalog, text: str) -> str | None:
    for cat, words in catalog.categories.items():
        if text in words or text == cat:
            return cat
    return None


def run_store_suite(ctx, catalog: Catalog, store: StoreData, scenarios: list[StoreScenario], *,
                    threshold: float, out_dir: Path | None = None, tags: set[str] | None = None,
                    batch_id: str | None = None, on_turn=None, stop_event=None,
                    shops: ShopSessions | None = None, source: str = "store", mode: str = "rules",
                    classifier=None) -> dict[str, Any]:
    batch_id = batch_id or datetime.now(UTC).strftime("store-%Y%m%dT%H%M%S")
    # Barge-in cases run only when asked for: they are a separate test type, not part of the regression baseline.
    if tags:
        selected = [s for s in scenarios if set(s.tags) & tags]
    else:
        selected = [s for s in scenarios if not any(t.startswith("barge_in") for t in s.tags)]
    records: list[dict[str, Any]] = []
    for scenario in selected:
        if stop_event is not None and stop_event.is_set():
            break
        run = run_store_scenario(ctx, catalog, store, scenario, source=source, mode=mode, classifier=classifier,
                                 batch_id=batch_id, on_turn=on_turn, shops=shops)
        if run.cancelled:
            # A stopped run is not evaluated and is not part of the suite's results.
            ctx.runs.finish(run.run_id, "error", {"cancelled": True, "passed": False, "checks": [],
                                                  "error": "stopped by the operator"})
            break
        audit = ctx.audit.list(run.run_id)
        checks, metrics = evaluate_store_run(catalog, store, run, audit)
        passed = run.error is None and all(c.passed for c in checks)
        status = "error" if run.error else ("passed" if passed else "failed")
        summary = {"passed": passed, "checks": [c.to_dict() for c in checks], "outcome": metrics.get("path"),
                   "barge_in": run.barge_in}
        if run.error:
            summary["error"] = run.error  # kept so the monitor can say why the case stopped
        ctx.runs.finish(run.run_id, status, summary)
        records.append({
            "run_id": run.run_id,
            "scenario_id": scenario.id,
            "language": "en",
            "tags": list(scenario.tags),
            "mode": "rules",
            "passed": passed,
            "status": status,
            "checks": [c.to_dict() for c in checks],
            "metrics": metrics,
            "wall_ms": run.wall_ms,
            "conversation": [
                {"seq": t.seq, "speaker": t.speaker, "language": t.language, "text_spoken": t.text_spoken,
                 "text_heard": t.text_heard, "act": t.act, "intent": t.intent, "latency_ms": t.latency_ms,
                 "agent": t.agent, "stages": t.stages}
                for t in run.turns
            ],
        })

    summary = analyse(records, aggregate(records, threshold))
    # Store runs have their own check names, so report every name that actually occurred.
    names = sorted({c["name"] for r in records for c in r["checks"]})
    summary["check_pass_rates"] = {
        n: round(sum(c["passed"] for r in records for c in r["checks"] if c["name"] == n)
                 / max(1, sum(1 for r in records for c in r["checks"] if c["name"] == n)), 4)
        for n in names
    }
    summary["by_agent_turns"] = _sum_agents(records)
    report = {
        "batch_id": batch_id,
        "domain": "store",
        "mode": "rules",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summary,
        "failures": [
            {"run_id": r["run_id"], "scenario_id": r["scenario_id"],
             "failed_checks": [{**c, "category": category_of(c["name"])} for c in r["checks"] if not c["passed"]]}
            for r in records if not r["passed"]
        ],
        "runs": [{k: v for k, v in r.items() if k != "conversation"} for r in records],
        "conversations": {r["scenario_id"]: r["conversation"] for r in records},
    }
    if out_dir:
        write_reports(report, out_dir)
    return report


def _sum_agents(records: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for r in records:
        for agent, n in (r["metrics"].get("turns_by_agent") or {}).items():
            totals[agent] = totals.get(agent, 0) + n
    return totals
