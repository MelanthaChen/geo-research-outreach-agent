"""Run/resume full-pool identity, GEO, and first-party contact validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.phase7 import (
    PHASE7_DB, export_outputs, integrity, preflight, run, select_remaining,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="Reconcile complete saved pool and strict contactable baseline")
    commands.add_parser("prepare", help="Persist a deterministic full-pool work order without fetching")
    run_parser = commands.add_parser("run", help="Run/resume bounded live processing")
    run_parser.add_argument("--database", default=str(PHASE7_DB))
    run_parser.add_argument("--batch-size", type=int, default=250)
    run_parser.add_argument("--request-cap", type=int, default=5000, help="Cumulative HTTP attempts across saved runs")
    run_parser.add_argument("--time-cap-seconds", type=int, default=7200, help="Cumulative processing seconds across saved runs")
    run_parser.add_argument("--chunk-request-budget", type=int, default=500)
    run_parser.add_argument("--request-timeout-seconds", type=float, default=12)
    run_parser.add_argument("--retry-count", type=int, default=1)
    run_parser.add_argument("--delay-between-requests-seconds", type=float, default=1)
    run_parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=1,
                            help="Maximum simultaneous company inspections; respects per-domain crawl ordering")
    run_parser.add_argument("--max-candidates", type=int,
                            help="Stop after this many additional candidate records; useful for bounded benchmarks")
    run_parser.add_argument("--stop-file", help="Stop cleanly after the active 25-candidate checkpoint finishes")
    run_parser.add_argument("--export-each-batch", action="store_true",
                            help="Refresh reproducible exports after each completed selection batch")
    export_parser = commands.add_parser("export", help="Regenerate strict evidence exports offline")
    export_parser.add_argument("--database", default=str(PHASE7_DB))
    status_parser = commands.add_parser("status", help="Show saved progress and SQLite integrity")
    status_parser.add_argument("--database", default=str(PHASE7_DB))
    args = parser.parse_args()

    if args.command == "preflight":
        result = preflight()
    elif args.command == "prepare":
        count = select_remaining()
        result = {"selected": count, "database": str(PHASE7_DB), "integrity": integrity()}
    elif args.command == "run":
        database = Path(args.database)
        result = run(db_path=database, batch_size=args.batch_size, request_cap=args.request_cap,
                     time_cap_seconds=args.time_cap_seconds, chunk_request_budget=args.chunk_request_budget,
                     request_timeout_seconds=args.request_timeout_seconds, retry_count=args.retry_count,
                     delay_between_requests_seconds=args.delay_between_requests_seconds,
                     concurrency=args.concurrency, max_candidates=args.max_candidates,
                     stop_file=Path(args.stop_file) if args.stop_file else None,
                     export_each_batch=args.export_each_batch)
        result["exports"] = export_outputs(db_path=database)
    elif args.command == "export":
        database = Path(args.database)
        result = {"exports": export_outputs(db_path=database), "integrity": integrity(database)}
    else:
        database = Path(args.database)
        result = {"integrity": integrity(database), "preflight": preflight(db_path=database)}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
