#!/usr/bin/env python3
"""Generate Phase 6C cohort, test mail-merge, and real eligibility exports."""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from outreach_agent.dashboard_data import DEFAULT_DB, load_dashboard_data  # noqa: E402
from outreach_agent.mail_merge import (  # noqa: E402
    DEFAULT_EXPORT_DIR, ExportLedger, make_all_company_export, make_eligibility_report,
    make_test_mail_merge, save_export,
)

CAMPAIGN_DB = ROOT / "data/phase5d_campaigns.db"


def active_campaign_id(path: Path) -> str:
    if not path.exists():
        return "PHASE6C-ELIGIBILITY-AUDIT"
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        row = db.execute("SELECT id FROM campaigns ORDER BY created_at DESC,id LIMIT 1").fetchone()
    return str(row[0]) if row else "PHASE6C-ELIGIBILITY-AUDIT"


def row_count(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--campaign-database", type=Path, default=CAMPAIGN_DB)
    parser.add_argument("--ledger", type=Path, default=None)
    args = parser.parse_args()

    data = load_dashboard_data(args.database)
    ledger = ExportLedger(args.ledger) if args.ledger else ExportLedger()
    all_csv = make_all_company_export(data, args.campaign_database)
    all_path, all_id = save_export(args.output_dir, "phase6c_all_companies.csv", all_csv)
    all_rows = row_count(all_path)
    ledger.record(campaign_id="COHORT", mode="ALL_COMPANIES", rows=all_rows, exclusions={}, file_id=all_id)

    test_csv = make_test_mail_merge("TEST-ONLY")
    test_path, test_id = save_export(args.output_dir, "phase6c_test_mail_merge.csv", test_csv)
    test_rows = row_count(test_path)
    ledger.record(campaign_id="TEST-ONLY", mode="TEST_MAIL_MERGE", rows=test_rows, exclusions={}, file_id=test_id)

    campaign_id = active_campaign_id(args.campaign_database)
    eligibility_csv, assessment = make_eligibility_report(data, campaign_id, args.campaign_database)
    eligibility_path, eligibility_id = save_export(args.output_dir, "phase6c_real_campaign_eligibility.csv", eligibility_csv)
    ledger.record(campaign_id=campaign_id, mode="REAL_ELIGIBILITY_REPORT",
                  rows=assessment["selected_count"], exclusions=assessment["exclusion_counts"], file_id=eligibility_id)

    real_path = None
    if assessment["included_count"]:
        from outreach_agent.mail_merge import MAIL_MERGE_COLUMNS, _csv_text
        real_csv = _csv_text(MAIL_MERGE_COLUMNS, assessment["rows"])
        real_path, real_id = save_export(args.output_dir, f"phase6c_real_mail_merge_{campaign_id[:12]}.csv", real_csv)
        ledger.record(campaign_id=campaign_id, mode="REAL_MAIL_MERGE_PREPARED", rows=assessment["included_count"],
                      exclusions=assessment["exclusion_counts"], file_id=real_id)

    print(f"Active cohort: {data['summary']['total_companies']} companies")
    print(f"All-company CSV: {all_path} ({all_rows} rows)")
    print(f"Synthetic test CSV: {test_path} ({test_rows} rows; expected exactly 2)")
    print(f"Real eligibility report: {eligibility_path} ({assessment['selected_count']} rows)")
    print(f"Real mail-merge candidates: {assessment['included_count']}; excluded companies: {assessment['excluded_count']}")
    for reason, count in assessment["exclusion_counts"].items():
        print(f"  {reason}: {count}")
    print(f"Real mail-merge CSV: {real_path if real_path else 'NOT GENERATED — no company passed all gates'}")
    print("Delivery status: no messages sent; exports remain Prepared only.")


if __name__ == "__main__":
    main()
