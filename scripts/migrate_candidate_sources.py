#!/usr/bin/env python3
"""Migrate the immutable company/hotel source stores into the Outreach cloud read model.

The SQLite inputs are opened read-only.  The target may be SQLite for isolated
tests or a PostgreSQL URL for staging/production migration.  The source mirror
tables preserve the complete source row as canonical JSON in addition to the
stable dashboard columns; research_records remains the lifecycle authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from outreach_agent.database import connect_database
from outreach_agent.geographic_eligibility import classify_geography
from outreach_agent.research_lifecycle import REGISTRY_DB, ingest_candidate_stores, init_registry


COMPANY_COLUMNS = (
    "candidate_id", "company_name", "website", "canonical_domain", "industry", "location",
    "source_name", "source_url", "source_record_id", "source_license_or_permission",
    "discovery_timestamp", "verification_status", "geo_eligibility_status", "geo_opportunity_status",
    "contact_status", "human_review_status", "outreach_ready_status", "verification_reason",
    "qualification_evidence", "fetch_status", "http_status", "evidence_json", "last_inspected_at",
    "source_batch",
)
HOTEL_COLUMNS = COMPANY_COLUMNS + (
    "property_type", "address", "chain_name", "chain_status", "amenities_json", "room_evidence_json",
    "guest_segments_json", "operating_evidence_status",
)
CONTACT_COLUMNS = ("candidate_id", "channel_type", "email", "channel_url", "source_url", "evidence", "purpose", "review_status")
HOTEL_CONTACT_COLUMNS = CONTACT_COLUMNS + ("discovered_at",)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_json(row: dict[str, Any]) -> str:
    return _json({key: row.get(key) for key in sorted(row)})


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlite(path: Path):
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _target_schema(target) -> None:
    target.executescript(
        """
        CREATE TABLE IF NOT EXISTS candidate_source_manifest (
          source_name TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_table TEXT NOT NULL,
          source_row_count INTEGER NOT NULL, migrated_row_count INTEGER NOT NULL DEFAULT 0,
          source_checksum TEXT NOT NULL, migrated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_companies (
          candidate_id TEXT PRIMARY KEY, company_name TEXT, website TEXT, canonical_domain TEXT,
          industry TEXT, location TEXT, source_name TEXT, source_url TEXT, source_record_id TEXT,
          source_license_or_permission TEXT, discovery_timestamp TEXT, verification_status TEXT,
          geo_eligibility_status TEXT, geo_opportunity_status TEXT, contact_status TEXT,
          human_review_status TEXT, outreach_ready_status TEXT, verification_reason TEXT,
          qualification_evidence TEXT, fetch_status TEXT, http_status TEXT, evidence_json TEXT,
          last_inspected_at TEXT, source_batch TEXT, source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_hotels (
          candidate_id TEXT PRIMARY KEY, company_name TEXT, website TEXT, canonical_domain TEXT,
          industry TEXT, location TEXT, source_name TEXT, source_url TEXT, source_record_id TEXT,
          source_license_or_permission TEXT, discovery_timestamp TEXT, verification_status TEXT,
          geo_eligibility_status TEXT, geo_opportunity_status TEXT, contact_status TEXT,
          human_review_status TEXT, outreach_ready_status TEXT, verification_reason TEXT,
          qualification_evidence TEXT, fetch_status TEXT, http_status TEXT, evidence_json TEXT,
          last_inspected_at TEXT, source_batch TEXT, property_type TEXT, address TEXT, chain_name TEXT,
          chain_status TEXT, amenities_json TEXT, room_evidence_json TEXT, guest_segments_json TEXT,
          operating_evidence_status TEXT, source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_company_provenance (
          candidate_id TEXT NOT NULL, source_name TEXT NOT NULL, source_record_id TEXT NOT NULL,
          source_url TEXT, source_license_or_permission TEXT, discovery_timestamp TEXT,
          source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL,
          PRIMARY KEY(candidate_id, source_name, source_record_id)
        );
        CREATE TABLE IF NOT EXISTS candidate_hotel_provenance (
          candidate_id TEXT NOT NULL, source_record_id TEXT NOT NULL, source_file TEXT NOT NULL,
          source_name TEXT, source_url TEXT, source_license_or_permission TEXT, source_dataset_url TEXT,
          discovery_timestamp TEXT, source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL,
          PRIMARY KEY(candidate_id, source_record_id, source_file)
        );
        CREATE TABLE IF NOT EXISTS candidate_company_contacts (
          candidate_id TEXT NOT NULL, channel_type TEXT NOT NULL, email TEXT NOT NULL, channel_url TEXT NOT NULL,
          source_url TEXT, evidence TEXT, purpose TEXT, review_status TEXT,
          source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL,
          PRIMARY KEY(candidate_id, channel_type, email, channel_url)
        );
        CREATE TABLE IF NOT EXISTS candidate_hotel_contacts (
          candidate_id TEXT NOT NULL, channel_type TEXT NOT NULL, email TEXT NOT NULL, channel_url TEXT NOT NULL,
          source_url TEXT, evidence TEXT, purpose TEXT, review_status TEXT, discovered_at TEXT,
          source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL,
          PRIMARY KEY(candidate_id, channel_type, email, channel_url)
        );
        CREATE TABLE IF NOT EXISTS candidate_company_screening (
          candidate_id TEXT PRIMARY KEY, screen_status TEXT, priority_score TEXT, category_group TEXT,
          chain_location_concern TEXT, rule_version TEXT, screened_at TEXT, manual_sample_class TEXT,
          manual_sample_note TEXT, website_domain_concern TEXT, priority_basis TEXT,
          source_row_json TEXT NOT NULL, source_row_checksum TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS candidate_companies_domain_idx ON candidate_companies(canonical_domain);
        CREATE INDEX IF NOT EXISTS candidate_hotels_domain_idx ON candidate_hotels(canonical_domain);
        CREATE INDEX IF NOT EXISTS candidate_companies_geo_idx ON candidate_companies(geo_eligibility_status);
        CREATE INDEX IF NOT EXISTS candidate_hotels_geo_idx ON candidate_hotels(geo_eligibility_status);
        """
    )


def _upsert(target, table: str, columns: list[str], row: dict[str, Any], key_columns: list[str]) -> None:
    values = [row.get(column) for column in columns]
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in key_columns)
    target.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({','.join(key_columns)}) DO UPDATE SET {updates}", values
    )


def _source_checksum(rows: list[dict[str, Any]]) -> str:
    return _checksum("\n".join(_row_json(row) for row in rows))


def _migrate_candidates(target, source_path: Path, *, hotel: bool, report: dict[str, Any]) -> None:
    source = _sqlite(source_path)
    prefix = "hotel" if hotel else "company"
    candidate_table = "candidate_hotels" if hotel else "candidate_companies"
    candidate_columns = list(HOTEL_COLUMNS if hotel else COMPANY_COLUMNS)
    contacts_table = "hotel_contact_channels" if hotel else "phase6f_contact_channels"
    source_candidates = [dict(row) for row in source.execute("SELECT * FROM candidates ORDER BY candidate_id")]
    source_name = f"{prefix}:candidates"
    for row in source_candidates:
        raw = _row_json(row)
        payload = {key: row.get(key) for key in candidate_columns}
        payload.update(source_row_json=raw, source_row_checksum=_checksum(raw))
        _upsert(target, candidate_table, candidate_columns + ["source_row_json", "source_row_checksum"], payload, ["candidate_id"])

    provenance_table = "hotel_provenance" if hotel else "candidate_provenance"
    mirror_provenance = "candidate_hotel_provenance" if hotel else "candidate_company_provenance"
    for row in source.execute(f"SELECT * FROM {provenance_table} ORDER BY candidate_id"):
        item = dict(row)
        raw = _row_json(item)
        if hotel:
            payload = {key: item.get(key) for key in ("candidate_id", "source_record_id", "source_file", "source_name", "source_url", "source_license_or_permission", "source_dataset_url", "discovery_timestamp")}
            keys = ["candidate_id", "source_record_id", "source_file"]
        else:
            payload = {key: item.get(key) for key in ("candidate_id", "source_name", "source_record_id", "source_url", "source_license_or_permission", "discovery_timestamp")}
            keys = ["candidate_id", "source_name", "source_record_id"]
        payload.update(source_row_json=raw, source_row_checksum=_checksum(raw))
        _upsert(target, mirror_provenance, list(payload), payload, keys)

    if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (contacts_table,)).fetchone():
        for row in source.execute(f"SELECT * FROM {contacts_table} ORDER BY candidate_id"):
            item = dict(row)
            raw = _row_json(item)
            fields = list(HOTEL_CONTACT_COLUMNS if hotel else CONTACT_COLUMNS)
            payload = {key: item.get(key) or "" for key in fields}
            payload.update(source_row_json=raw, source_row_checksum=_checksum(raw))
            _upsert(target, "candidate_hotel_contacts" if hotel else "candidate_company_contacts", fields + ["source_row_json", "source_row_checksum"], payload, ["candidate_id", "channel_type", "email", "channel_url"])

    if not hotel and source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase6e_screening'").fetchone():
        for row in source.execute("SELECT * FROM phase6e_screening ORDER BY candidate_id"):
            item = dict(row)
            raw = _row_json(item)
            payload = {key: item.get(key) for key in ("candidate_id", "screen_status", "priority_score", "category_group", "chain_location_concern", "rule_version", "screened_at", "manual_sample_class", "manual_sample_note", "website_domain_concern", "priority_basis")}
            payload.update(source_row_json=raw, source_row_checksum=_checksum(raw))
            _upsert(target, "candidate_company_screening", list(payload), payload, ["candidate_id"])

    report["sources"][source_name] = {"rows": len(source_candidates), "checksum": _source_checksum(source_candidates)}
    report["counts"][prefix] = len(source_candidates)
    source.close()


def _reconcile(target, report: dict[str, Any]) -> None:
    for prefix, table in (("company", "candidate_companies"), ("hotel", "candidate_hotels")):
        rows = [dict(row) for row in target.execute(f"SELECT * FROM {table} ORDER BY candidate_id")]
        domains = Counter(str(row.get("canonical_domain") or "").casefold() for row in rows if row.get("canonical_domain"))
        duplicate_domains = {domain: count for domain, count in domains.items() if count > 1}
        field_checksums = {}
        for field in ("company_name", "website", "canonical_domain", "location", "verification_status", "geo_eligibility_status", "contact_status", "human_review_status", "outreach_ready_status"):
            field_checksums[field] = _checksum("\n".join(f"{row['candidate_id']}={row.get(field) or ''}" for row in rows))
        report["reconciliation"][prefix] = {
            "target_rows": len(rows),
            "duplicate_domains": duplicate_domains,
            "field_checksums": field_checksums,
            "missing_candidate_ids_in_provenance": int(target.execute(f"SELECT COUNT(*) FROM {'candidate_hotel_provenance' if prefix == 'hotel' else 'candidate_company_provenance'} p LEFT JOIN {table} c ON c.candidate_id=p.candidate_id WHERE c.candidate_id IS NULL").fetchone()[0]),
            "missing_candidate_ids_in_contacts": int(target.execute(f"SELECT COUNT(*) FROM {'candidate_hotel_contacts' if prefix == 'hotel' else 'candidate_company_contacts'} p LEFT JOIN {table} c ON c.candidate_id=p.candidate_id WHERE c.candidate_id IS NULL").fetchone()[0]),
        }
    report["reconciliation"]["research_records"] = {
        "total": int(target.execute("SELECT COUNT(*) FROM research_records").fetchone()[0]),
        "companies": int(target.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='business_company'").fetchone()[0]),
        "hotels": int(target.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='hotel_property'").fetchone()[0]),
        "owner_controlled_tests": int(target.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='owner_controlled_test'").fetchone()[0]),
    }
    try:
        queue_rows = int(target.execute("SELECT COUNT(*) FROM full_screening_queue").fetchone()[0])
    except Exception:
        queue_rows = None
    report["reconciliation"]["queue"] = {"rows": queue_rows}


def migrate(*, business_db: Path, hotel_db: Path, registry_db: str | Path = REGISTRY_DB, report_path: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"schema_version": "candidate-source-reconciliation/v1", "dry_run": dry_run, "sources": {}, "counts": {}, "reconciliation": {}}
    target_registry = registry_db
    temporary_target = None
    if dry_run:
        temporary_target = tempfile.NamedTemporaryFile(prefix="candidate-source-dry-run-", suffix=".db", delete=False)
        temporary_target.close()
        target_registry = Path(temporary_target.name)
    init_registry(target_registry)
    target = connect_database(target_registry)
    if isinstance(target, sqlite3.Connection):
        target.row_factory = sqlite3.Row
    _target_schema(target)
    _migrate_candidates(target, business_db, hotel=False, report=report)
    _migrate_candidates(target, hotel_db, hotel=True, report=report)
    # Populate the existing lifecycle/readiness model without resetting consent or invitation fields.
    if not dry_run:
        target.commit()
        target.close()
        report["lifecycle_upsert"] = ingest_candidate_stores(business_db=business_db, hotel_db=hotel_db, registry_db=registry_db)
        target = connect_database(registry_db)
        if isinstance(target, sqlite3.Connection):
            target.row_factory = sqlite3.Row
        _target_schema(target)
        _reconcile(target, report)
        target.commit()
    else:
        report["lifecycle_upsert"] = {"dry_run": True}
        _reconcile(target, report)
    target.close()
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if temporary_target is not None:
        Path(temporary_target.name).unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-db", type=Path, default=Path("data/phase7_candidates.db"))
    parser.add_argument("--hotel-db", type=Path, default=Path("data/hotel_candidates.db"))
    parser.add_argument("--registry", default=str(REGISTRY_DB), help="SQLite path or PostgreSQL URL")
    parser.add_argument("--report", type=Path, default=Path("/tmp/candidate_source_reconciliation.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = migrate(business_db=args.business_db, hotel_db=args.hotel_db, registry_db=args.registry, report_path=args.report, dry_run=args.dry_run)
    print(json.dumps({"dry_run": report["dry_run"], "counts": report["counts"], "lifecycle_upsert": report["lifecycle_upsert"], "report": str(args.report)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
