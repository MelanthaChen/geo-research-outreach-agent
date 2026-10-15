#!/usr/bin/env python3
"""Run the offline Phase 6G contact audit and one bounded comparison sample."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter

from outreach_agent.phase6f import PHASE6F_DB
from outreach_agent.phase6g import EVALUATION_DB, OUT, contact_audit_rows, export_all, failure_rows, run_evaluation, write_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("audit", help="Generate deterministic offline audit CSVs")
    evaluate = commands.add_parser("evaluate", help="Recheck one resumable sample of prior DNS/TLS/timeout failures and ambiguous identities")
    evaluate.add_argument("--batch-size", type=int, default=10)
    evaluate.add_argument("--request-cap", type=int, default=90)
    evaluate.add_argument("--time-cap-seconds", type=int, default=600)
    evaluate.add_argument("--request-timeout-seconds", type=float, default=12)
    evaluate.add_argument("--database", default=str(EVALUATION_DB))
    commands.add_parser("report", help="Refresh report and offline exports")
    args = parser.parse_args()
    if args.command == "audit":
        result = export_all()
        result["contact_classes"] = dict(Counter(r["channel_classification"] for r in contact_audit_rows()))
        result["failure_categories"] = dict(Counter(r["failure_category"] for r in failure_rows()))
        write_report()
    elif args.command == "evaluate":
        result = run_evaluation(path=__import__("pathlib").Path(args.database), batch_size=args.batch_size,
                                request_cap=args.request_cap, time_cap_seconds=args.time_cap_seconds,
                                request_timeout_seconds=args.request_timeout_seconds)
        export_all()
        write_report(evaluation_path=__import__("pathlib").Path(args.database))
    else:
        result = export_all()
        write_report()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
