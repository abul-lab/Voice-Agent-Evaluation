"""Command-line interface.

  voiceshop scenarios                      list the test cases
  voiceshop eval [--tags a,b] [--out DIR]  run the suite, write reports, exit non-zero below the gate
  voiceshop serve [--host H] [--port P]    start the live monitor and storefront
"""

from __future__ import annotations

import argparse
import logging
import sys
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

    v = sub.add_parser("serve", help="start the live monitor and storefront")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=8765)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        if args.command == "scenarios":
            for s in load_store_scenarios(SCENARIOS_PATH):
                print(f"{s.id:<44} {','.join(s.tags)}")
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
                "shops": ShopSessions(),  # one storefront session store, shared by the monitor and the shop page
            }
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
            print(f"monitor on http://{args.host}:{args.port}  "
                  f"(mutating API {'enabled' if app.api_key else 'disabled'})")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
                app.close()
            return 0
    except VoiceShopError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
