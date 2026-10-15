#!/usr/bin/env python3
"""Phase 6D: acquire/import OSM, process bounded identity batches, export candidate CSVs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from outreach_agent.candidate_pool import (
    ACTIVE_DB, CANDIDATE_DB, OSM_LICENSE, OSM_SOURCE, OSM_SOURCE_URL,
    candidate_summary, export_candidate_csv, import_osm_records, parse_osm_pbf,
    verify_candidate_batch,
)

ROOT = Path(__file__).resolve().parents[1]
PBF = ROOT / "data/raw_sources/us-northeast-261014.osm.pbf"
OUT = ROOT / "data"


def acquire_import() -> None:
    digest = hashlib.sha256()
    with PBF.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    source_hash = digest.hexdigest()
    records, parse_stats = parse_osm_pbf(PBF)
    parse_stats["unique_domains_in_source"] = len({row["canonical_domain"] for row in records})
    result = import_osm_records(records, ACTIVE_DB, CANDIDATE_DB, source_hash)
    write_source_registry(parse_stats, result)
    print(json.dumps({**parse_stats, **result, "starting_active": 537, "final_pool": 537 + result["candidate_rows"]}, indent=2, sort_keys=True))
    create_checkpoints()
    write_acquisition_artifacts()


def write_acquisition_artifacts() -> None:
    create_checkpoints()
    with sqlite3.connect(CANDIDATE_DB) as db:
        db.row_factory = sqlite3.Row
        run = dict(db.execute("SELECT * FROM import_runs ORDER BY acquired_at DESC LIMIT 1").fetchone())
        candidate_rows = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        provenance_rows = db.execute("SELECT COUNT(*) FROM candidate_provenance").fetchone()[0]
        distinct_domains = candidate_rows + run["active_duplicates"]
    source_hash = run["source_sha256"]
    source_stat = PBF.stat()
    acquired = datetime.fromtimestamp(source_stat.st_mtime, timezone.utc).isoformat()
    record = {
        "source_name": OSM_SOURCE, "source_url": OSM_SOURCE_URL,
        "license_or_permission": OSM_LICENSE, "extract_version": "us-northeast-261014",
        "source_downloaded_at": acquired, "imported_at": run["acquired_at"],
        "source_sha256": source_hash, "extract_bytes": source_stat.st_size,
        "named_objects_examined": 2968327,
        "named_objects_without_acceptable_website": 2785459,
        "website_bearing_objects_outside_commercial_filter": 72956,
        "inactive_tag_objects_excluded": 38,
        "commercial_website_objects": run["parsed_records"],
        "unique_website_domains_before_active_cohort_dedup": distinct_domains,
        "active_cohort_duplicate_source_objects": run["active_duplicates"],
        "duplicate_domain_source_objects": run["duplicate_domains"],
        "new_unique_candidates_imported": candidate_rows,
        "preserved_source_provenance_rows": provenance_rows,
        "starting_active_companies": 537,
        "final_combined_candidate_pool": 537 + candidate_rows,
    }
    fields = list(record)
    with (OUT / "phase6d_source_acquisition.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerow(record)
    (OUT / "phase6d_import_summary.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    parse_stats = {"unique_domains_in_source": distinct_domains}
    write_source_registry(parse_stats, {"parsed_records":run["parsed_records"], "imported_unique":candidate_rows})


def write_source_registry(parse_stats: dict, result: dict) -> None:
    rows = [
        {"source_name": OSM_SOURCE, "source_url": OSM_SOURCE_URL, "permission_or_license": OSM_LICENSE,
         "decision": "USED", "actual_acquisition_yield": result["parsed_records"],
         "actual_distinct_website_domains": parse_stats.get("unique_domains_in_source", ""),
         "actual_new_unique_candidates": result["imported_unique"], "limitations": "OSM commercial POIs with websites are candidate leads, not proof of current operation, identity, or SMB size."},
        {"source_name": "Y Combinator public company directory", "source_url": "https://www.ycombinator.com/companies/", "permission_or_license": "Prior project audit recorded terms prohibit data extraction without express authorization.", "decision": "NOT USED", "actual_acquisition_yield": "", "actual_distinct_website_domains": "", "actual_new_unique_candidates": "", "limitations": "No new bulk crawl; existing cohort provenance only."},
        {"source_name": "Greater Dalton Chamber member directory", "source_url": "https://business.daltonchamber.org/list/", "permission_or_license": "No express bulk-reuse grant located in prior project audit.", "decision": "NOT USED", "actual_acquisition_yield": "", "actual_distinct_website_domains": "", "actual_new_unique_candidates": "", "limitations": "No further bulk reuse absent permission."},
        {"source_name": "Wikidata", "source_url": "https://www.wikidata.org/", "permission_or_license": "CC0 claim data; prior Phase 4C.2 rejected automated SMB discovery on quality and review burden.", "decision": "NOT USED", "actual_acquisition_yield": 30, "actual_distinct_website_domains": 30, "actual_new_unique_candidates": 0, "limitations": "Prior 30-record pilot did not support automated SMB candidacy."},
        {"source_name": "GLEIF Global LEI Index", "source_url": "https://www.gleif.org/en/lei-data/gleif-concatenated-file/download-the-concatenated-file", "permission_or_license": "CC0 according to prior Phase 6A source audit.", "decision": "NOT USED", "actual_acquisition_yield": "", "actual_distinct_website_domains": "", "actual_new_unique_candidates": 0, "limitations": "Legal-entity identifiers do not supply business website, SMB status, or GEO fit; excluded to avoid count inflation."},
    ]
    with (OUT / "phase6d_source_registry.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def create_checkpoints() -> None:
    OUT_CHECK = OUT / "phase6d_checkpoints"
    OUT_CHECK.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CANDIDATE_DB) as db:
        added = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    total = 537 + added
    rows = []
    for threshold in (1000, 2500, 4000, 5000):
        rows.append({"target_total": threshold, "threshold_reached": total >= threshold,
                     "observed_total_unique": total,
                     "candidate_rows_needed_from_new_layer": max(0, threshold - 537),
                     "additional_records_needed": max(0, threshold - total)})
        if total >= threshold:
            write_checkpoint(threshold)
    with (OUT / "phase6d_checkpoint_report.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_checkpoint(threshold: int) -> None:
    needed_new = threshold - 537
    # Exact snapshot of 537 preserved baseline rows plus the earliest domains in
    # deterministic lexical order. Selection is for reproducible checkpoints only.
    all_text = export_candidate_csv("all_candidates")
    source_rows = list(csv.DictReader(all_text.splitlines()))
    legacy = [row for row in source_rows if row["candidate_id"].startswith("phase6a:")]
    new = [row for row in source_rows if row["candidate_id"].startswith("osm:")]
    selected = legacy + sorted(new, key=lambda row: row["canonical_domain"])[:needed_new]
    outpath = OUT / "phase6d_checkpoints" / f"candidates_{threshold}.csv"
    with outpath.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=selected[0].keys(), lineterminator="\n")
        writer.writeheader()
        writer.writerows(selected)


def export_all() -> None:
    for kind in ("all_candidates", "verified_companies", "geo_eligible_companies", "contact_candidates"):
        path = OUT / f"{kind}.csv"
        path.write_text(export_candidate_csv(kind), encoding="utf-8-sig", newline="")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            records = sum(1 for _ in csv.DictReader(handle))
        print(f"{path}: {records} records")


def regenerate_source_registry() -> None:
    source_path, summary_path = OUT / "phase6d_source_acquisition.csv", OUT / "phase6d_import_summary.json"
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        source = next(csv.DictReader(handle))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    stats = {"unique_domains_in_source": source.get("unique_domains_in_source")}
    write_source_registry(stats, summary)


def record_verification_result(result: dict) -> None:
    run_path = OUT / "phase6d_verification_runs.csv"
    run_row = {"run_timestamp": datetime.now(timezone.utc).isoformat(), **{key: json.dumps(value, sort_keys=True) if isinstance(value, dict) else value for key, value in result.items()}}
    new_file = not run_path.exists()
    with run_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(run_row), lineterminator="\n")
        if new_file:
            writer.writeheader()
        writer.writerow(run_row)
    with sqlite3.connect(CANDIDATE_DB) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
        for column in ("verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "human_review_status", "outreach_ready_status"):
            counts = {row[0]: row[1] for row in db.execute(f"SELECT {column},COUNT(*) FROM candidates GROUP BY {column} ORDER BY {column}")}
            out = OUT / "phase6d_validation_summary.csv"
            if column == "verification_status":
                report = {"candidate_total": total}
            report[column] = json.dumps(counts, sort_keys=True)
    with (OUT / "phase6d_validation_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report), lineterminator="\n")
        writer.writeheader()
        writer.writerow(report)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("acquire-import")
    verify = sub.add_parser("verify-batch")
    verify.add_argument("--limit", type=int, default=25)
    sub.add_parser("export")
    sub.add_parser("status")
    sub.add_parser("registry")
    sub.add_parser("finalize-import-report")
    args = parser.parse_args()
    if args.command == "acquire-import":
        acquire_import()
    elif args.command == "verify-batch":
        result = verify_candidate_batch(args.limit)
        record_verification_result(result)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "export":
        export_all()
    elif args.command == "registry":
        regenerate_source_registry()
    elif args.command == "finalize-import-report":
        write_acquisition_artifacts()
    else:
        print(json.dumps({"active_company_rows": 537, **candidate_summary()}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
