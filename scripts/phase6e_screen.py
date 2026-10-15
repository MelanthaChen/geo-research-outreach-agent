#!/usr/bin/env python3
"""Offline full-pool screening and explicitly bounded Phase 6E verification."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3

from outreach_agent.candidate_pool import verify_candidate_batch
from outreach_agent.phase6e_screening import (
    OUTPUT, PHASE6E_DB, audit_profile, create_working_copy,
    export_phase6e, label_calibration_sample, screen_all, stratified_sample, write_sample,
)


def verification_ids(limit: int) -> list[str]:
    with sqlite3.connect(PHASE6E_DB) as db:
        return [row[0] for row in db.execute("""SELECT c.candidate_id FROM candidates c JOIN phase6e_screening s USING(candidate_id)
            WHERE c.verification_status='DISCOVERED_UNVERIFIED' ORDER BY s.priority_score DESC,s.category_group,c.canonical_domain LIMIT ?""", (limit,))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit")
    sample = sub.add_parser("sample")
    sample.add_argument("--per-category", type=int, default=12)
    sub.add_parser("review-sample")
    sub.add_parser("screen")
    verify = sub.add_parser("verify")
    verify.add_argument("--budget", type=int, default=25)
    verify.add_argument("--request-budget", type=int, default=100, help="hard maximum HTTP request attempts including redirects and retries")
    sub.add_parser("export")
    args = parser.parse_args()
    if args.command == "audit":
        result = audit_profile()
        for filename, rows in (("phase6e_source_category_distribution.csv", result["categories"]), ("phase6e_geography_distribution.csv", result["geography_top_100"])):
            if rows:
                with (OUTPUT / filename).open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
    elif args.command == "sample":
        create_working_copy()
        rows = stratified_sample(per_category=args.per_category)
        write_sample(rows)
        result = {"sample_size": len(rows), "strata": len({row["calibration_stratum"] for row in rows}), "path": "data/phase6e_calibration_sample.csv"}
    elif args.command == "review-sample":
        rows = stratified_sample(per_category=20)
        result = {"sample_size": len(rows), "manual_desk_review_labels": label_calibration_sample(rows)}
    elif args.command == "screen":
        result = screen_all(include_existing=True)
    elif args.command == "verify":
        if not 1 <= args.budget <= 50 or not 1 <= args.request_budget <= 1000:
            parser.error("--budget must be 1..50 company sites and --request-budget must be 1..1000 HTTP attempts")
        create_working_copy()
        ids = verification_ids(args.budget)
        # Verify on a copy only; Phase 6D DB and active company databases are never written.
        result = verify_candidate_batch(len(ids), candidate_db=PHASE6E_DB, candidate_ids=ids,
                                       request_budget=args.request_budget) if ids else {"attempted": 0, "remaining": 0}
        run_path = OUTPUT / "phase6e_verification_runs.csv"
        if run_path.exists():
            with run_path.open(encoding="utf-8-sig", newline="") as source:
                prior = list(csv.DictReader(source))
        else:
            prior = []
        row = {"attempted": result.get("attempted", 0), "runtime_seconds": result.get("runtime_seconds", 0), "requests_attempted": result.get("requests_attempted", 0), "requests_succeeded": result.get("requests_succeeded", 0), "requests_failed": result.get("requests_failed", 0), "request_budget": result.get("request_budget", args.request_budget), "request_budget_exhausted": result.get("request_budget_exhausted", False), "retries": result.get("retries", 0), "counts": json.dumps(result.get("counts", {}), sort_keys=True)}
        fields = list(row)
        with run_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(prior)
            writer.writerow(row)
    else:
        result = export_phase6e()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
