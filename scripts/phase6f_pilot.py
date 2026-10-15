#!/usr/bin/env python3
"""Run and resume the bounded Phase 6F website verification pilot."""

from __future__ import annotations

import argparse
import json

from outreach_agent.phase6f import PHASE6F_DB, TOP5K, dashboard_summary, export_outputs, integrity, pilot_report, run_pilot, select_pilot, save_selection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select")
    select.add_argument("--limit", type=int, default=500)
    run = sub.add_parser("run")
    run.add_argument("--limit", type=int, default=500)
    run.add_argument("--batch-size", type=int, default=20)
    run.add_argument("--request-cap", type=int, default=3000)
    run.add_argument("--time-cap-seconds", type=int, default=10800)
    run.add_argument("--cost-cap-usd", type=float, default=0.0, help="paid-service cap; Phase 6F permits $0.00 only")
    run.add_argument("--database", default=str(PHASE6F_DB))
    sub.add_parser("export")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "select":
        rows = select_pilot(args.limit)
        save_selection(rows)
        result = {"selected": len(rows), "file": "data/phase6f_selected_500.csv"}
    elif args.command == "run":
        result = run_pilot(limit=args.limit, batch_size=args.batch_size, request_cap=args.request_cap,
                           time_cap_seconds=args.time_cap_seconds, cost_cap_usd=args.cost_cap_usd,
                           db_path=__import__("pathlib").Path(args.database))
    elif args.command == "export":
        result = export_outputs()
    else:
        result = {"integrity_check": integrity(), "summary": dashboard_summary(), "report": pilot_report()}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
