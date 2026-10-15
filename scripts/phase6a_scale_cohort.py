"""Offline progress reporting and integrity validation for the Phase 6A cohort.

Automated ingestion remains disabled until an explicit source authorization and
source-specific adapter have been reviewed. This command never performs network
requests and never writes to the company or campaign databases.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB = DATA / "phase6a_experimental.db"
BASELINE_DB = DATA / "phase4f_experimental.db"
FROZEN_DB = DATA / "phase3c1_validation_77.db"
CAMPAIGN_DB = DATA / "phase5d_campaigns.db"
WEBSITE_EXPORT = DATA / "phase4f_website_verification.csv"
PRIOR_WEBSITE_EXPORT = DATA / "phase4e_website_verification.csv"
TARGET = 5000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_digest(connection: sqlite3.Connection, table: str, max_id: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    if max_id is None:
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY id")
    else:
        rows = connection.execute(f"SELECT * FROM {table} WHERE id<=? ORDER BY id", (max_id,))
    count = 0
    for row in rows:
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def read_verification() -> dict[int, str]:
    result: dict[int, str] = {}
    for path in (PRIOR_WEBSITE_EXPORT, WEBSITE_EXPORT):
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("company_id") and row.get("verification_status"):
                    result[int(row["company_id"])] = row["verification_status"].upper()
    return result


def progress_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    verification = read_verification()
    with sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        companies = db.execute("SELECT * FROM companies ORDER BY id").fetchall()
        if len({row["normalized_domain"] for row in companies}) != len(companies):
            raise RuntimeError("Duplicate normalized domains found in Phase 6A database")
        source_names: dict[int, set[str]] = {}
        for row in db.execute("SELECT company_id,source_name,source_type FROM discovery_records"):
            source_names.setdefault(row["company_id"], set()).add(row["source_name"] or row["source_type"] or "Unknown")
        contact_types: dict[int, set[str]] = {}
        for row in db.execute("SELECT company_id,channel_type,review_status,source_url,channel_url,email FROM contacts"):
            contact_types.setdefault(row["company_id"], set()).add(row["channel_type"] or "UNCLASSIFIED")
        suitable = {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "CONTACT_FORM"}
        source_counter: Counter[str] = Counter()
        verification_counter: Counter[str] = Counter()
        eligibility_counter: Counter[str] = Counter()
        geo_counter: Counter[str] = Counter()
        high_counter: Counter[str] = Counter()
        email_ids: set[int] = set()
        form_ids: set[int] = set()
        for company in companies:
            labels = source_names.get(company["id"], {"Unknown"})
            for source in labels:
                source_counter[source] += 1
            status = verification.get(company["id"], "NOT_IN_EXPORT")
            verification_counter[status] += 1
            eligibility_counter[(company["eligibility"] or "NEEDS_REVIEW").upper()] += 1
            geo = (company["geo_opportunity"] or "UNKNOWN").upper()
            geo_counter[geo] += 1
            if (company["priority_tier"] or "").upper() == "HIGH":
                for source in labels:
                    high_counter[source] += 1
            types = contact_types.get(company["id"], set())
            if types.intersection(suitable):
                # Candidate coverage upper-bound from saved contact types;
                # first-party applicability is separately guarded by dashboard.
                if types.intersection({"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL"}):
                    email_ids.add(company["id"])
                if "CONTACT_FORM" in types:
                    form_ids.add(company["id"])
        snapshots = db.execute("SELECT fetch_status,COUNT(*) n FROM website_snapshots GROUP BY fetch_status").fetchall()
        processing_errors = db.execute("SELECT COUNT(*) FROM website_snapshots WHERE fetch_status IN ('FETCH_FAILED','ACCESS_BLOCKED','ROBOTS_DENIED')").fetchone()[0]
        discoveries = db.execute("SELECT COUNT(*) FROM discovery_records").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM companies WHERE COALESCE(review_status,'PENDING')='PENDING'").fetchone()[0]
        contact_failed = db.execute("SELECT COUNT(*) FROM companies WHERE contact_status='FAILED'").fetchone()[0]
        size_missing = db.execute("SELECT COUNT(*) FROM companies WHERE company_size_value IS NULL").fetchone()[0]
        contact_counts = db.execute("SELECT channel_type,COUNT(*) n FROM contacts GROUP BY channel_type").fetchall()
        total = len(companies)
        progress = [{
            "snapshot_mtime_utc": datetime.fromtimestamp(DB.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "baseline_companies": total,
            "new_discovered_candidates": 0,
            "rejected_duplicate_candidates": "NOT_MEASURED_PHASE6A",
            "accepted_unique_companies": 0,
            "total_cohort_companies": total,
            "target_companies": TARGET,
            "remaining_to_target": max(0, TARGET - total),
            "verified_websites": verification_counter.get("VERIFIED", 0),
            "known_unresolved_or_nonverified_websites": sum(
                count for status, count in verification_counter.items()
                if status not in {"VERIFIED", "NOT_IN_EXPORT"}
            ),
            "website_identity_not_assessed_in_saved_exports": verification_counter.get("NOT_IN_EXPORT", 0),
            "eligible_companies": eligibility_counter.get("ELIGIBLE", 0),
            "ineligible_companies": eligibility_counter.get("INELIGIBLE", 0) + eligibility_counter.get("REJECTED", 0),
            "needs_review_companies": eligibility_counter.get("NEEDS_REVIEW", 0),
            "high_geo_opportunity_companies": geo_counter.get("HIGH", 0),
            "companies_with_saved_suitable_email_type_upper_bound": len(email_ids),
            "companies_with_saved_contact_form_type_upper_bound": len(form_ids),
            "companies_missing_size_evidence": size_missing,
            "pending_human_review_companies": pending,
            "contact_discovery_failures": contact_failed,
            "website_snapshot_error_status_count": processing_errors,
            "source_provenance_rows": discoveries,
            "source_permissions_cleared_for_new_bulk_ingestion": 0,
            "estimated_remaining_work": "NOT_ESTIMABLE_WITHOUT_APPROVED_SOURCE",
        }]
        yields = []
        for source, count in sorted(source_counter.items()):
            yields.append({"source_name": source, "existing_unique_companies": count,
                           "high_priority_companies": high_counter[source], "phase6a_new_unique": 0,
                           "estimated_additional_yield": "NOT_ESTIMATED", "permission_status": "SEE_PHASE6A_SOURCE_INVENTORY"})
        yields.extend({"source_name": f"website:{row['fetch_status'] or 'UNKNOWN'}", "existing_unique_companies": row["n"],
                       "high_priority_companies": "", "phase6a_new_unique": "", "estimated_additional_yield": "",
                       "permission_status": "PROCESSING_STATUS"} for row in snapshots)
        yields.extend({"source_name": f"contact_type:{row['channel_type'] or 'UNCLASSIFIED'}", "existing_unique_companies": row["n"],
                       "high_priority_companies": "", "phase6a_new_unique": "", "estimated_additional_yield": "",
                       "permission_status": "SAVED_CONTACT_RECORDS"} for row in contact_counts)
    return progress, yields


def report() -> None:
    progress, yields = progress_rows()
    for row in progress:
        print(json.dumps(row, sort_keys=True))
    write_csv(DATA / "phase6a_expansion_progress.csv", list(progress[0]), progress)
    write_csv(DATA / "phase6a_source_yields.csv", list(yields[0]), yields)
    write_csv(DATA / "phase6a_company_quality_summary.csv", list(progress[0]), progress)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def validate() -> None:
    if not all(path.exists() for path in (DB, BASELINE_DB, FROZEN_DB, CAMPAIGN_DB)):
        raise RuntimeError("Expected Phase 6A, Phase 4F, frozen validation, and campaign DB files")
    with sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True) as current, \
            sqlite3.connect(f"file:{BASELINE_DB.resolve()}?mode=ro", uri=True) as baseline:
        for table in ("companies", "discovery_records", "website_snapshots", "website_pages", "contacts", "contact_extraction_runs"):
            max_id = baseline.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
            if table_digest(current, table, max_id) != table_digest(baseline, table):
                raise RuntimeError(f"Phase 6A snapshot rows diverged from preserved Phase 4F baseline in {table}")
        if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Phase 6A SQLite integrity_check failed")
    print(json.dumps({"phase6a_companies": 537, "phase4f_baseline_sha256": sha256(BASELINE_DB),
                      "phase6a_snapshot_sha256": sha256(DB), "frozen_77_sha256": sha256(FROZEN_DB),
                      "campaign_sidecar_sha256": sha256(CAMPAIGN_DB), "campaign_sidecar_read_only_checked": True,
                      "status": "VALID"}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report", help="Generate offline progress/source-yield reports")
    commands.add_parser("validate", help="Check cohort snapshot integrity and preserved inputs")
    for name in ("discover", "verify", "qualify", "discover-contacts", "resume"):
        command = commands.add_parser(name, help="Reserved until source permission/adapter approval")
        command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "report":
        report()
    elif args.command == "validate":
        validate()
    else:
        parser.error(f"{args.command} is disabled: no source is currently cleared for new automated bulk ingestion")


if __name__ == "__main__":
    main()
