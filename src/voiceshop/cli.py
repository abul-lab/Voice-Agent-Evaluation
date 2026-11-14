"""Command-line interface.

  voiceshop scenarios                      list the test cases
  voiceshop eval [--tags a,b] [--out DIR]  run the suite, write reports, exit non-zero below the gate
  voiceshop serve [--host H] [--port P]    start the live monitor and storefront
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from voiceshop.catalog.model import Catalog
from voiceshop.catalog.shop import ShopSessions
from voiceshop.catalog.store import StoreData
from voiceshop.catalog.storerun import load_store_scenarios, run_store_suite
from voiceshop.config import load_settings
from voiceshop.context import build_context
from voiceshop.errors import VoiceShopError
from voiceshop.logging_utils import configure_logging
from voiceshop.monitor.server import MonitorApp, make_server

log = logging.getLogger("voiceshop.cli")
ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = ROOT / "data" / "catalog" / "northwind_outdoor.json"
SCENARIOS_PATH = ROOT / "scenarios" / "store_scenarios.json"
PASS_THRESHOLD = 0.9  # share of fully passing cases needed for the gate; the director sets the real value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voiceshop", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=None, help="SQLite database path")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("scenarios", help="list the test cases")

    e = sub.add_parser("eval", help="run the test suite (smoke, targeted or full regression)")
    e.add_argument("--tags", default="", help="comma-separated tags, e.g. smoke or targeted; empty runs all")
    e.add_argument("--out", type=Path, default=ROOT / "results" / "latest")

    g = sub.add_parser("regress", help="compare a report with the saved baseline (release gate)")
    g.add_argument("--baseline", type=Path, default=ROOT / "tests" / "baseline" / "store.json")
    g.add_argument("--report", type=Path, default=ROOT / "results" / "latest" / "report.json")

    b = sub.add_parser("baseline", help="write the baseline from a clean report (review the diff in PRs)")
    b.add_argument("--report", type=Path, default=ROOT / "results" / "latest" / "report.json")
    b.add_argument("--out", type=Path, default=ROOT / "tests" / "baseline" / "store.json")

    j = sub.add_parser("judge", help="score the agent's replies in a report with an LLM judge")
    j.add_argument("--backend", choices=("ollama", "gemini"), default="ollama")
    j.add_argument("--model", default=None)
    j.add_argument("--report", type=Path, default=ROOT / "results" / "latest" / "report.json")
    j.add_argument("--out", type=Path, default=ROOT / "results" / "latest" / "judged.json")
    j.add_argument("--allow-partial", action="store_true", help="write the result even if some calls failed")

    c = sub.add_parser("calibrate", help="measure how often the judge agrees with the human labels")
    c.add_argument("--backend", choices=("ollama", "gemini"), default="ollama")
    c.add_argument("--model", default=None)
    c.add_argument("--items", type=Path, default=ROOT / "tests" / "judge" / "calibration.json")
    c.add_argument("--out", type=Path, default=ROOT / "results" / "judge" / "calibration.json")
    c.add_argument("--allow-partial", action="store_true", help="write the result even if some calls failed")

    v = sub.add_parser("serve", help="start the live monitor and storefront")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8765)
    return p


def _judge_command(args) -> int:
    from voiceshop.evaluation.judge import (
        JudgeError,
        assert_complete,
        build_facts,
        calibrate,
        judge_report,
        make_backend,
    )
    from voiceshop.evaluation.regression import load_json, write_json

    backend = make_backend(args.backend, args.model)
    facts = build_facts(Catalog.load(DATA_PATH), StoreData(DATA_PATH))
    try:
        if args.command == "judge":
            result = judge_report(load_json(args.report), backend, facts)
            if not args.allow_partial:
                assert_complete(result)
            write_json(args.out, result)
            s = result["summary"]
            print(f"judged {s['turns_judged']} agent replies with {backend.name}:{backend.model} "
                  f"(failures {s['failures']})")
            print(f"accuracy {s['accuracy']}  helpfulness {s['helpfulness']}  tone {s['tone']}  "
                  f"invented facts {s['invented_facts']}")
            print(f"written to {args.out}")
            return 0
        result = calibrate(backend, load_json(args.items)["items"], facts)
        if not args.allow_partial:
            assert_complete(result)
        write_json(args.out, result)
        print(f"calibration with {backend.name}:{backend.model}: agreement {result['agreement']} "
              f"over {result['items']} items (failures {result['failures']})")
        print(f"written to {args.out}")
        return 0
    except JudgeError as exc:
        print(f"judge error: {exc}", file=sys.stderr)
        return 3


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        if args.command == "scenarios":
            for s in load_store_scenarios(SCENARIOS_PATH):
                print(f"{s.id:<44} {','.join(s.tags)}")
            return 0

        if args.command in ("judge", "calibrate"):
            return _judge_command(args)

        if args.command == "regress":
            from voiceshop.evaluation.regression import compare, load_json

            findings = compare(load_json(args.baseline), load_json(args.report))
            if not findings:
                print("regression gate: PASS (no case regressed against the baseline)")
                return 0
            print(f"regression gate: FAIL ({len(findings)} finding(s))")
            for f in findings:
                print(f"  [{f.rule}] {f.scenario_id or ''} {f.detail}".rstrip())
            return 1

        if args.command == "baseline":
            from voiceshop.evaluation.regression import load_json, make_baseline, write_json

            write_json(args.out, make_baseline(load_json(args.report)))
            print(f"baseline written to {args.out}")
            return 0

        ctx = build_context(settings, args.db)
        catalog, store = Catalog.load(DATA_PATH), StoreData(DATA_PATH)

        if args.command == "eval":
            wanted = {x.strip() for x in args.tags.split(",") if x.strip()}
            scenarios = load_store_scenarios(SCENARIOS_PATH)
            report = run_store_suite(ctx, catalog, store, scenarios, threshold=PASS_THRESHOLD,
                                     out_dir=args.out, tags=wanted or None)
            s = report["summary"]
            print(f"runs={s['runs']} passed={s['passed']} failed={s['failed']} errors={s['errors']} "
                  f"pass_rate={s['pass_rate']:.1%}")
            for f in report["failures"]:
                for ch in f["failed_checks"]:
                    print(f"  FAIL {f['scenario_id']}: {ch['name']}: {ch['detail']}")
            print(f"reports: {args.out / 'report.md'}")
            return 0 if s["gate_passed"] else 1

        if args.command == "serve":
            store_bundle = {
                "catalog": catalog,
                "store": store,
                "scenarios": load_store_scenarios(SCENARIOS_PATH),
                "shops": ShopSessions(ctx.db_path),  # durable storefront sessions, shared by the monitor and the shop page
            }
            interrupted = ctx.runs.reconcile_interrupted()
            if interrupted:
                log.warning("runs interrupted by a previous stop were marked as errors", extra={"count": interrupted})
            app = MonitorApp(
                ctx,
                api_key=settings.api_key,
                max_concurrent_runs=settings.max_concurrent_runs,
                max_body_bytes=settings.max_body_bytes,
                local_ui_no_key=settings.local_ui_no_key,
                store_bundle=store_bundle,
                settings_path=settings.db_path.parent / "monitor_settings.json",
            )
            server = make_server(app, args.host, args.port)

            def _stop(signum, _frame):  # stop taking requests; the drain happens below, on the main thread
                log.info("stop requested", extra={"signal": signum})
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGINT, _stop)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, _stop)
            print(f"monitor on http://{args.host}:{args.port}  "
                  f"(mutating API {'enabled' if app.api_key else 'disabled'})")
            try:
                server.serve_forever()
            finally:
                clean = app.drain(timeout_s=30.0)
                if not clean:
                    log.warning("some runs did not stop within 30 seconds")
                server.server_close()
                app.close()
                ctx.runs.reconcile_interrupted()  # anything still marked running is recorded as interrupted
                log.info("stopped", extra={"clean": clean})
            return 0
    except VoiceShopError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
