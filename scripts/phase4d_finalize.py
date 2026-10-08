"""Finalize and validate the isolated Phase 4D source expansion exports."""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from outreach_agent.database import create_db_engine, session_factory
from outreach_agent.discovery import CompanyCandidate, DiscoverySource
from outreach_agent.models import Company, DiscoveryRecord
from outreach_agent.normalization import normalize_domain
from outreach_agent.services import ingest
from outreach_agent.wikidata import is_third_party_profile, write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
EXPERIMENT_DB = DATA / "phase4d_experimental.db"
BASELINE_EXPORT = DATA / "phase3d_company_review_77.csv"
REVIEW_EXPORT = DATA / "phase4d_expanded_company_review.csv"


class ReplaySource(DiscoverySource):
    def __init__(self, source_type: str, candidates: list[CompanyCandidate]):
        self.source_type = source_type
        self.path = Path("phase4d-saved-provenance")
        self.candidates = candidates

    def discover(self):
        yield from self.candidates


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def preserve_correct_yc_source_labels() -> None:
    baseline_domains = {normalize_domain(row["website"]) for row in csv_rows(BASELINE_EXPORT)}
    engine = create_db_engine(f"sqlite:///{EXPERIMENT_DB}")
    with session_factory(engine)() as session:
        for record, company in session.execute(
            select(DiscoveryRecord, Company).join(Company, Company.id == DiscoveryRecord.company_id)
        ):
            if record.source_type != "yc_public" or company.normalized_domain in baseline_domains:
                continue
            record.source_name = "Y Combinator Public Startup Directory"
            raw = json.loads(record.raw_data or "{}")
            raw["source_name"] = record.source_name
            record.raw_data = json.dumps(raw, sort_keys=True)
        session.commit()
    engine.dispose()


def replay_idempotency() -> dict[str, dict[str, int]]:
    engine = create_db_engine(f"sqlite:///{EXPERIMENT_DB}")
    with session_factory(engine)() as session:
        grouped: dict[str, list[CompanyCandidate]] = {}
        for record in session.scalars(select(DiscoveryRecord).order_by(DiscoveryRecord.id)).all():
            grouped.setdefault(record.source_type, []).append(CompanyCandidate.model_validate(json.loads(record.raw_data)))
        summaries: dict[str, dict[str, int]] = {}
        for source_type, candidates in sorted(grouped.items()):
            summary = ingest(session, ReplaySource(source_type, candidates))
            summaries[source_type] = {
                "processed": summary.processed, "created": summary.created,
                "duplicates": summary.duplicates, "provenance_added": summary.provenance_added,
                "provenance_existing": summary.provenance_existing,
            }
    engine.dispose()
    if any(result["created"] or result["provenance_added"] for result in summaries.values()):
        raise RuntimeError(f"saved-provenance replay was not idempotent: {summaries}")
    return summaries


def build_exports() -> None:
    baseline_domains = {normalize_domain(row["website"]) for row in csv_rows(BASELINE_EXPORT)}
    rows = csv_rows(REVIEW_EXPORT)
    for row in rows:
        row["cohort_membership"] = "BASELINE_PRESERVED" if row["normalized_domain"] in baseline_domains else "NEW_EXPERIMENTAL"
        row["website_url_is_third_party_profile"] = str(is_third_party_profile(row["website"])).lower()
    fields = list(rows[0])
    write_csv_atomic(DATA / "phase4d_expanded_company_cohort.csv", fields, rows)

    yc = [row for row in rows if "Y Combinator" in row["discovery_sources"]]
    chamber = [row for row in rows if "Greater Dalton Chamber" in row["discovery_sources"]]
    new_yc = [row for row in yc if row["cohort_membership"] == "NEW_EXPERIMENTAL"]
    new_chamber = [row for row in chamber if row["cohort_membership"] == "NEW_EXPERIMENTAL"]
    with sqlite3.connect(EXPERIMENT_DB) as connection:
        source_records = dict(connection.execute("select source_type, count(*) from discovery_records group by source_type"))
        distinct_by_source = {
            source_type: count for source_type, count in connection.execute(
                "select d.source_type, count(distinct c.normalized_domain) from discovery_records d join companies c on c.id=d.company_id group by d.source_type"
            )
        }
        duplicate_domains = connection.execute(
            "select count(*) from (select normalized_domain from companies group by normalized_domain having count(*) > 1)"
        ).fetchone()[0]
        provenance_rows = connection.execute("select count(*) from discovery_records").fetchone()[0]
        companies = connection.execute("select count(*) from companies").fetchone()[0]

    audit_fields = [
        "source", "baseline_provenance_records", "baseline_unique_companies", "controlled_run_processed",
        "controlled_run_domain_duplicates", "controlled_run_new_unique_records", "combined_source_provenance_records",
        "combined_source_unique_domains", "directory_routes_covered", "pagination_coverage",
        "website_url_availability", "permission_observation", "additional_accessible_records_estimate",
        "expected_new_unique_companies_observed", "deduplication_notes",
    ]
    audit_rows = [
        {
            "source": "YC official public startup directory",
            "baseline_provenance_records": "30", "baseline_unique_companies": "30",
            "controlled_run_processed": "121", "controlled_run_domain_duplicates": "10",
            "controlled_run_new_unique_records": str(len(new_yc)),
            "combined_source_provenance_records": str(source_records.get("yc_public", 0)),
            "combined_source_unique_domains": str(distinct_by_source.get("yc_public", 0)),
            "directory_routes_covered": "Consumer (50), Health-Tech (49), Consumer Electronics (22) current listings",
            "pagination_coverage": "One static industry/category page per route; no offset/page cursor; max 250 collected links and 50 valid candidates per run",
            "website_url_availability": "100% of accepted rows include a directory-supplied HTTP(S) website; identity/first-party status not verified in this discovery phase",
            "permission_observation": "Official YC domain; current robots.txt returned 200 and allows static /companies/industry/* paths; query-driven /companies?* routes are disallowed and were not used. No data-reuse license/legal opinion inferred.",
            "additional_accessible_records_estimate": "At least 111 new unique domains observed in 3 category runs; remaining active, website-bearing records beyond these routes are not enumerated",
            "expected_new_unique_companies_observed": str(len(new_yc)),
            "deduplication_notes": "10/121 (8.3%) repeated normalized domains across the three current category runs; source IDs/profile URLs retained; missing reported team size remains visible.",
        },
        {
            "source": "Greater Dalton Chamber GrowthZone directory",
            "baseline_provenance_records": "50", "baseline_unique_companies": "47",
            "controlled_run_processed": "60", "controlled_run_domain_duplicates": "50",
            "controlled_run_new_unique_records": str(len(new_chamber)),
            "combined_source_provenance_records": str(source_records.get("growthzone_public", 0)),
            "combined_source_unique_domains": str(distinct_by_source.get("growthzone_public", 0)),
            "directory_routes_covered": "Alphabetical listing routes A-Z; public directory also exposes category pages",
            "pagination_coverage": "No page cursor implemented; run stops at 60 cards. Root page lists category routes; these were not crawled in this bounded expansion.",
            "website_url_availability": "Baseline listing candidates and accepted new candidates had a URL; the adapter accepts social/profile domains too. At least one new record points to linkedin.com/in and is flagged for review, not accepted as a verified company website.",
            "permission_observation": "Current robots.txt returned 200 and allows the directory routes. Public member listings are intended for directory browsing; no explicit automated reuse grant or separate terms approval was established.",
            "additional_accessible_records_estimate": "10 additional unique domains observed when the run limit increased from the original 50 provenance records to 60; members beyond the cap/category results remain unenumerated",
            "expected_new_unique_companies_observed": str(len(new_chamber)),
            "deduplication_notes": "50/60 (83.3%) candidates matched existing normalized domains; 10 new domains added. The baseline Chamber source had 50 provenance rows for 47 unique domains (3 repeated-domain provenance rows within Chamber).",
        },
    ]
    write_csv_atomic(DATA / "phase4d_source_capacity_audit.csv", audit_fields, audit_rows)
    if companies != len(rows) or duplicate_domains:
        raise RuntimeError(f"cohort export or domain uniqueness mismatch: db={companies}, export={len(rows)}, duplicate_domains={duplicate_domains}")
    if provenance_rows != sum(source_records.values()):
        raise RuntimeError("provenance count mismatch")
    print(json.dumps({"companies": companies, "new_yc": len(new_yc), "new_chamber": len(new_chamber), "provenance_rows": provenance_rows, "duplicate_domain_groups": duplicate_domains}, sort_keys=True))


def main() -> None:
    preserve_correct_yc_source_labels()
    summaries = replay_idempotency()
    print(json.dumps({"offline_idempotency_replay": summaries}, sort_keys=True))
    build_exports()


if __name__ == "__main__":
    main()
