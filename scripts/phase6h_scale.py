#!/usr/bin/env python3
"""Reconcile, prepare, run, and resume bounded Phase 6H batches."""

from __future__ import annotations

import argparse
import json

from outreach_agent.phase6h import (
    PHASE6H_DB, TOP5K, dashboard_summary, export_outputs, initialize, integrity,
    reconcile, run, select_remaining, write_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("preflight", help="Read-only reconcile shortlist, prior results, and integrity")
    subs.add_parser("prepare", help="Create continuation backup and persist deterministic workload")
    run_parser = subs.add_parser("run", help="Run/resume bounded live verification")
    run_parser.add_argument("--batch-size", type=int, default=250)
    run_parser.add_argument("--request-cap", type=int, default=10000)
    run_parser.add_argument("--time-cap-seconds", type=int, default=36000)
    run_parser.add_argument("--chunk-request-budget", type=int, default=500)
    run_parser.add_argument("--request-timeout-seconds", type=float, default=12)
    run_parser.add_argument("--retry-count", type=int, default=1)
    run_parser.add_argument("--delay-between-requests-seconds", type=float, default=1)
    run_parser.add_argument("--database", default=str(PHASE6H_DB))
    export_parser = subs.add_parser("export", help="Regenerate deterministic exports and report offline")
    export_parser.add_argument("--database", default=str(PHASE6H_DB))
    status_parser = subs.add_parser("status", help="Show checkpoint and store integrity")
    status_parser.add_argument("--database", default=str(PHASE6H_DB))
    args = parser.parse_args()
    if args.command == "preflight":
        result = {"reconciliation": reconcile(), "phase6e_integrity": "checked", "phase6f_integrity": "checked"}
        # Open read-only handles through their exported paths to avoid a write-capable check.
        from outreach_agent.phase6e_screening import PHASE6E_DB
        from outreach_agent.phase6f import PHASE6F_DB
        import sqlite3
        for label, path in (("phase6e_integrity", PHASE6E_DB), ("phase6f_integrity", PHASE6F_DB)):
            with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
                result[label] = db.execute("PRAGMA integrity_check").fetchone()[0]
    elif args.command == "prepare":
        initialize()
        result = {"selected": select_remaining(), "database": str(PHASE6H_DB), "integrity": integrity()}
    elif args.command == "run":
        from pathlib import Path
        result = run(db_path=Path(args.database), batch_size=args.batch_size, request_cap=args.request_cap,
                     time_cap_seconds=args.time_cap_seconds, chunk_request_budget=args.chunk_request_budget,
                     request_timeout_seconds=args.request_timeout_seconds, retry_count=args.retry_count,
                     delay_between_requests_seconds=args.delay_between_requests_seconds)
        result["integrity"] = integrity(Path(args.database))
        result["exports"] = export_outputs(db_path=Path(args.database))
        result["report"] = str(write_report(db_path=Path(args.database)))
    elif args.command == "export":
        from pathlib import Path
        result = {"summary": dashboard_summary(Path(args.database)), "integrity": integrity(Path(args.database)),
                  "exports": export_outputs(db_path=Path(args.database)), "report": str(write_report(db_path=Path(args.database)))}
    else:
        from pathlib import Path
        result = {"summary": dashboard_summary(Path(args.database)), "integrity": integrity(Path(args.database))}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
