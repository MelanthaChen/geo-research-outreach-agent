"""Generate deterministic, read-only Phase 4G review and scale-readiness exports."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from outreach_agent.wikidata import write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB = DATA / "phase4f_experimental.db"
REVIEW_FIELDS = [
    "company_id", "company_name", "website", "discovery_source", "industry", "company_size",
    "company_size_evidence_source", "company_size_category", "geography", "website_fetch_status",
    "identity_verification_status", "eligibility", "geo_opportunity", "research_priority", "evidence_confidence", "evidence_links",
    "missing_evidence", "required_review_action", "company_review_status", "identity_review_status",
    "eligibility_review_status", "geo_review_status", "contact_channel_status", "contact_channel_review_status",
    "outreach_readiness_status", "outreach_approval_status", "contact_channel_evidence_links", "reviewer_notes", "reviewed_by",
]
IDENTITY_PATHS = (DATA / "phase4e_website_verification.csv", DATA / "phase4f_website_verification.csv")
SUITABLE_CHANNELS = {
    "PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL",
    "NAMED_PERSON_EMAIL", "CONTACT_FORM", "SALES_MARKETING_EMAIL",
}
LEGACY_BASELINE_COMPANIES = 77
FIRST_PASS_COMPANIES = 339
FIRST_PASS_CRAWL_SECONDS = 1367.14
FIRST_PASS_REQUEST_LOWER_BOUND = 1265
CACHED_REPLAY_COMPANIES = 339
CACHED_REPLAY_SECONDS = 0.64


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_identity_status() -> dict[int, str]:
    result: dict[int, str] = {}
    for path in IDENTITY_PATHS:
        for row in read_csv(path):
            if row.get("company_id") and row.get("verification_status"):
                result[int(row["company_id"])] = row["verification_status"].upper()
    return result


def _priority_key(row: dict[str, str]) -> tuple[int, int, int, int, int, int]:
    identity_rank = 0 if row["identity_verification_status"] == "VERIFIED" else 1
    eligibility_rank = {"ELIGIBLE": 0, "INELIGIBLE": 1, "NEEDS_REVIEW": 2}.get(row["eligibility"], 3)
    geo_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "UNKNOWN": 3}.get(row["geo_opportunity"], 4)
    priority_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NEEDS_REVIEW": 3}.get(row["research_priority"], 4)
    confidence = row.get("_confidence", "LOW")
    evidence_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(confidence, 3)
    return identity_rank, eligibility_rank, geo_rank, priority_rank, evidence_rank, int(row["company_id"])


def classify_review_tasks(
    *, identity_status: str, eligibility: str, geo_opportunity: str, company_review_status: str,
    contact_status: str, contactability_status: str, pending_suitable_channels: int,
) -> dict[str, str]:
    identity = "PENDING" if identity_status != "VERIFIED" else "NOT_REQUIRED_MACHINE_VERIFIED"
    eligibility_task = "PENDING" if eligibility in {"NEEDS_REVIEW", "UNKNOWN", ""} else "MACHINE_SUPPORTED_PENDING_COMPANY_REVIEW" if company_review_status == "PENDING" else "NOT_REQUIRED"
    geo_task = "PENDING" if geo_opportunity in {"UNKNOWN", ""} else "MACHINE_SCORED_PENDING_COMPANY_REVIEW" if company_review_status == "PENDING" else "NOT_REQUIRED"
    if pending_suitable_channels and identity_status != "VERIFIED":
        contact_task = "WAITING_FOR_IDENTITY_VERIFICATION"
    elif pending_suitable_channels and eligibility != "ELIGIBLE":
        contact_task = "WAITING_FOR_ELIGIBILITY_REVIEW"
    elif pending_suitable_channels:
        contact_task = "PENDING"
    elif contact_status == "NOT_RUN":
        contact_task = "NOT_DISCOVERED"
    elif contact_status in {"FAILED", "BLOCKED"} or contactability_status == "FETCH_FAILED":
        contact_task = "RESEARCH_FAILED_OR_BLOCKED"
    else:
        contact_task = "NO_PENDING_SUITABLE_CHANNEL"
    return {
        "identity_review_status": identity,
        "eligibility_review_status": eligibility_task,
        "geo_review_status": geo_task,
        "contact_channel_review_status": contact_task,
        # This workflow never creates/approves outreach. Keep the explicit hold visible.
        "outreach_approval_status": "NOT_GRANTED",
    }


def classify_outreach_readiness(*, identity_status: str, eligibility: str, company_review_status: str,
                                contact_channel_review_status: str, contactability_status: str,
                                contact_status: str) -> str:
    if identity_status != "VERIFIED":
        return "BLOCKED_IDENTITY_REVIEW"
    if eligibility != "ELIGIBLE":
        return "BLOCKED_ELIGIBILITY_REVIEW"
    if company_review_status != "APPROVED":
        return "BLOCKED_COMPANY_REVIEW"
    if contact_channel_review_status == "PENDING":
        return "BLOCKED_CONTACT_CHANNEL_REVIEW"
    if contactability_status == "READY_FOR_REVIEW":
        return "CHANNEL_AVAILABLE"
    if contact_status == "NOT_RUN":
        return "CONTACT_CHANNEL_NOT_DISCOVERED"
    if contact_status in {"FAILED", "BLOCKED"}:
        return "CONTACT_DISCOVERY_FAILED_OR_BLOCKED"
    return "NO_SUITABLE_CHANNEL_OR_UNRESOLVED"


def missing_evidence(company: sqlite3.Row, identity_status: str, snapshot: sqlite3.Row | None) -> list[str]:
    missing: list[str] = []
    if identity_status != "VERIFIED":
        missing.append("first_party_identity_verification")
    if not company["industry"]:
        missing.append("industry")
    if not company["location"]:
        missing.append("geography")
    if company["company_size_value"] is None or company["company_size_value"] <= 0:
        missing.append("employee_size_evidence")
    if company["eligibility"] in {None, "NEEDS_REVIEW"}:
        missing.append("eligibility_evidence")
    if company["geo_opportunity"] in {None, "unknown", "UNKNOWN"}:
        missing.append("geo_opportunity_evidence")
    if snapshot is None:
        missing.append("website_inspection")
    elif snapshot["fetch_status"] in {"FETCH_FAILED", "ROBOTS_DENIED", "BLOCKED"}:
        missing.append("usable_website_content")
    return missing


def build_review_rows(connection: sqlite3.Connection, identity_statuses: dict[int, str]) -> list[dict[str, str]]:
    connection.row_factory = sqlite3.Row
    companies = connection.execute("SELECT * FROM companies ORDER BY id").fetchall()
    if len(companies) != 537:
        raise ValueError(f"expected 537 Phase 4F companies, found {len(companies)}")
    rows: list[dict[str, str]] = []
    for company in companies:
        company_id = company["id"]
        discoveries = connection.execute(
            "SELECT source_type, source_name, source_url, source_identifier, raw_data FROM discovery_records WHERE company_id=? ORDER BY id",
            (company_id,),
        ).fetchall()
        snapshots = connection.execute(
            "SELECT * FROM website_snapshots WHERE company_id=? ORDER BY fetched_at DESC, id DESC LIMIT 1", (company_id,)
        ).fetchone()
        contacts = connection.execute(
            "SELECT * FROM contacts WHERE company_id=? ORDER BY id", (company_id,)
        ).fetchall()
        sources = sorted({d["source_name"] or d["source_type"] for d in discoveries})
        source_urls = {d["source_url"] for d in discoveries if d["source_url"]}
        for discovery in discoveries:
            try:
                raw = json.loads(discovery["raw_data"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            if raw.get("source_directory_url"):
                source_urls.add(raw["source_directory_url"])
        if snapshots:
            for field in ("requested_url", "final_url", "canonical_url"):
                if snapshots[field]:
                    source_urls.add(snapshots[field])
        identity = identity_statuses.get(company_id, "BASELINE_NOT_RECHECKED")
        eligibility = (company["eligibility"] or "NEEDS_REVIEW").upper()
        geo = (company["geo_opportunity"] or "UNKNOWN").upper()
        priority = (company["priority_tier"] or "NEEDS_REVIEW").upper()
        evidence = (company["evidence_confidence"] or "LOW").upper()
        suitable = [c for c in contacts if c["channel_type"] in SUITABLE_CHANNELS]
        pending_suitable = [c for c in suitable if c["review_status"] == "PENDING"]
        tasks = classify_review_tasks(
            identity_status=identity, eligibility=eligibility, geo_opportunity=geo,
            company_review_status=company["review_status"], contact_status=company["contact_status"],
            contactability_status=company["contactability_status"], pending_suitable_channels=len(pending_suitable),
        )
        outreach_readiness = classify_outreach_readiness(
            identity_status=identity, eligibility=eligibility, company_review_status=company["review_status"],
            contact_channel_review_status=tasks["contact_channel_review_status"],
            contactability_status=company["contactability_status"], contact_status=company["contact_status"],
        )
        missing = missing_evidence(company, identity, snapshots)
        actions: list[str] = []
        if tasks["identity_review_status"] == "PENDING":
            actions.append("VERIFY_COMPANY_WEBSITE")
        if tasks["eligibility_review_status"] == "PENDING":
            actions.append("REVIEW_ELIGIBILITY")
        if tasks["geo_review_status"] == "PENDING":
            actions.append("REVIEW_GEO_OPPORTUNITY")
        if tasks["contact_channel_review_status"] == "PENDING":
            actions.append("REVIEW_CONTACT_CHANNEL")
        if company["review_status"] == "PENDING" and not actions:
            actions.append("CONFIRM_MACHINE_FINDINGS")
        links = sorted(source_urls | {company["website"]})
        channel_urls = sorted({c["source_url"] or c["channel_url"] for c in suitable if c["source_url"] or c["channel_url"]})
        rows.append({
            "company_id": str(company_id), "company_name": company["company_name"], "website": company["website"],
            "discovery_source": "; ".join(sources), "industry": company["industry"] or "",
            "company_size": str(company["company_size_value"]) if company["company_size_value"] is not None else "",
            "company_size_evidence_source": company["company_size_source"] or "unknown",
            "company_size_category": company["company_size_category"] or "UNKNOWN",
            "geography": company["location"] or "",
            "website_fetch_status": snapshots["fetch_status"] if snapshots else "NOT_INSPECTED",
            "identity_verification_status": identity, "eligibility": eligibility, "geo_opportunity": geo,
            "research_priority": priority, "evidence_confidence": evidence, "evidence_links": " | ".join(links),
            "missing_evidence": "; ".join(missing), "required_review_action": "; ".join(actions) or "NONE",
            "company_review_status": company["review_status"], **tasks,
            "contact_channel_status": company["contact_status"],
            "outreach_readiness_status": outreach_readiness,
            "contact_channel_evidence_links": " | ".join(channel_urls),
            "reviewer_notes": company["review_notes"] or "", "reviewed_by": company["reviewed_by"] or "",
            "_confidence": evidence, "_company_size_source": company["company_size_source"] or "unknown",
            "_company_size_category": company["company_size_category"] or "UNKNOWN",
            "_contact_pending_count": str(len(pending_suitable)),
            "_contact_suitable_count": str(len(suitable)),
        })
    rows.sort(key=_priority_key)
    return rows


def _source_groups(connection: sqlite3.Connection) -> dict[int, set[str]]:
    groups: dict[int, set[str]] = defaultdict(set)
    for row in connection.execute("SELECT company_id,source_type FROM discovery_records ORDER BY company_id,id"):
        if row["source_type"] == "yc_public":
            groups[row["company_id"]].add("YC")
        elif row["source_type"] == "growthzone_public":
            groups[row["company_id"]].add("Greater Dalton Chamber")
        else:
            groups[row["company_id"]].add(row["source_type"] or "Other")
    return groups


def build_diversity_rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    connection.row_factory = sqlite3.Row
    companies = connection.execute("SELECT * FROM companies ORDER BY id").fetchall()
    groups = _source_groups(connection)
    rows: list[dict[str, str]] = []

    def add(dimension: str, group: str, category: str, count: int, denominator: int) -> None:
        rows.append({"dimension": dimension, "source_group": group, "category": category, "company_count": str(count),
                     "source_group_total": str(denominator), "percent_of_source_group": f"{(100*count/denominator if denominator else 0):.2f}"})

    memberships = {"YC": {i for i, names in groups.items() if "YC" in names},
                   "Greater Dalton Chamber": {i for i, names in groups.items() if "Greater Dalton Chamber" in names}}
    memberships["All"] = {c["id"] for c in companies}
    company_by_id = {c["id"]: c for c in companies}
    for group, ids in memberships.items():
        denominator = len(ids)
        for label, count in Counter(company_by_id[i]["industry_normalized"] or "Unknown" for i in ids).most_common():
            add("industry", group, label, count, denominator)
        for label, count in Counter(company_by_id[i]["location"] or "Unknown" for i in ids).most_common():
            add("geography_exact_location", group, label, count, denominator)
        size = Counter("HAS_DIRECTORY_EMPLOYEE_COUNT" if company_by_id[i]["company_size_value"] and company_by_id[i]["company_size_value"] > 0 else "MISSING" for i in ids)
        for label, count in sorted(size.items()):
            add("employee_size_evidence", group, label, count, denominator)
        high = sum(company_by_id[i]["priority_tier"] == "HIGH" for i in ids)
        add("high_priority_concentration", group, "HIGH", high, denominator)
        for label, count in Counter(company_by_id[i]["priority_tier"] or "NEEDS_REVIEW" for i in ids).items():
            add("research_priority", group, label, count, denominator)
    for label, count in sorted(Counter(" + ".join(sorted(groups.get(c["id"], {"Unknown"}))) for c in companies).items()):
        add("source_membership_union", "All", label, count, len(companies))
    return rows


def project_scale(cohort_size: int, *, measured_companies: int = FIRST_PASS_COMPANIES,
                  measured_runtime_seconds: float = FIRST_PASS_CRAWL_SECONDS,
                  minimum_requests: int = FIRST_PASS_REQUEST_LOWER_BOUND,
                  review_basis_size: int = 460, review_counts: dict[str, int] | None = None) -> dict[str, str]:
    if min(cohort_size, measured_companies, review_basis_size) <= 0 or measured_runtime_seconds < 0 or minimum_requests < 0:
        raise ValueError("projection inputs must be nonnegative and cohort sizes positive")
    review_counts = review_counts or {}
    return {
        "company_count": str(cohort_size),
        "processed_company_count": str(cohort_size),
        "review_population_size": str(review_basis_size),
        "review_task_rate_basis_size": str(review_basis_size),
        "processing_runtime_estimate_minutes": f"{cohort_size * measured_runtime_seconds / measured_companies / 60:.2f}",
        "website_requests_minimum_estimate": str(math.ceil(cohort_size * minimum_requests / measured_companies)),
        "runtime_basis": f"Measured first-pass website processing: {measured_companies} companies, {measured_runtime_seconds:.2f}s",
        "request_count_basis": f"Extrapolated from at least {minimum_requests} inferred request calls in {measured_companies} companies; actual retries/redirects may be higher",
        "identity_review_task_estimate": str(round(cohort_size * review_counts.get("identity", 0) / review_basis_size)),
        "eligibility_review_task_estimate": str(round(cohort_size * review_counts.get("eligibility", 0) / review_basis_size)),
        "geo_review_task_estimate": str(round(cohort_size * review_counts.get("geo", 0) / review_basis_size)),
        "contact_channel_review_task_estimate": "NOT_EXTRAPOLATED_BOUNDED_CONTACT_SAMPLE" if review_counts.get("contact") is None else str(round(cohort_size * review_counts["contact"] / review_basis_size)),
        "company_review_decision_estimate": str(round(cohort_size * review_counts.get("company", 0) / review_basis_size)),
        "human_review_minutes": "NOT_ESTIMATED_NO_OBSERVED_REVIEW_DURATION",
        "assumptions": "Runtime extrapolated from Phase 4F first-pass; review rates from 460 Phase 4E/4F additions (IDs 78-537); contact excluded because enrichment was a 10-company sample; not an SLA or population-representative estimate",
    }


def build_scale_rows(review_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    additions = [r for r in review_rows if int(r["company_id"]) > LEGACY_BASELINE_COMPANIES]
    counts = {
        "identity": sum(r["identity_review_status"] == "PENDING" for r in additions),
        "eligibility": sum(r["eligibility_review_status"] == "PENDING" for r in additions),
        "geo": sum(r["geo_review_status"] == "PENDING" for r in additions),
        # Contact processing was intentionally limited to ten selected companies; it is not a cohort rate.
        "contact": None,
        "company": sum(r["company_review_status"] == "PENDING" for r in additions),
    }
    result: list[dict[str, str]] = []
    for size in (1000, 2500, 5000):
        result.append(project_scale(size, review_counts=counts))
    # Include directly observed offline cache replay; no network attempt was made.
    result.append({
        "company_count": str(CACHED_REPLAY_COMPANIES),
        "processed_company_count": str(CACHED_REPLAY_COMPANIES),
        "review_population_size": str(len(review_rows)),
        "review_task_rate_basis_size": str(len(review_rows)),
        "processing_runtime_estimate_minutes": f"{CACHED_REPLAY_SECONDS/60:.4f}",
        "website_requests_minimum_estimate": "0",
        "runtime_basis": "Measured Phase 4F cached replay for 339 companies: 0.64s",
        "request_count_basis": "Observed 339 cache hits and zero fetches on replay",
        "identity_review_task_estimate": str(sum(r["identity_review_status"] == "PENDING" for r in review_rows)),
        "eligibility_review_task_estimate": str(sum(r["eligibility_review_status"] == "PENDING" for r in review_rows)),
        "geo_review_task_estimate": str(sum(r["geo_review_status"] == "PENDING" for r in review_rows)),
        "contact_channel_review_task_estimate": str(sum(r["contact_channel_review_status"] == "PENDING" for r in review_rows)),
        "company_review_decision_estimate": str(sum(r["company_review_status"] == "PENDING" for r in review_rows)),
        "human_review_minutes": "NOT_ESTIMATED_NO_OBSERVED_REVIEW_DURATION",
        "assumptions": "Historical measured result, not a new crawl; qualification/export work was included in runtime",
    })
    return result


def write_outputs(db_path: Path = DB, output_dir: Path = DATA) -> dict[str, int]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        identity = load_identity_status()
        review_rows = build_review_rows(connection, identity)
        diversity_rows = build_diversity_rows(connection)
        scales = build_scale_rows(review_rows)
        write_csv_atomic(output_dir / "phase4g_review_queue.csv", REVIEW_FIELDS, review_rows)
        views = {
            "phase4g_high_priority_company_review.csv": [r for r in review_rows if r["identity_verification_status"] == "VERIFIED" and r["eligibility"] == "ELIGIBLE" and r["research_priority"] == "HIGH" and r["company_review_status"] == "PENDING"],
            "phase4g_ambiguous_identity_review.csv": [r for r in review_rows if r["identity_verification_status"] in {"AMBIGUOUS_IDENTITY", "INSUFFICIENT_EVIDENCE", "ACCESS_BLOCKED", "FETCH_FAILED", "ROBOTS_DENIED", "REJECTED_MISMATCH", "THIRD_PARTY_URL"}],
            "phase4g_legacy_identity_unassessed.csv": [r for r in review_rows if r["identity_verification_status"] == "BASELINE_NOT_RECHECKED"],
            "phase4g_missing_qualification_evidence.csv": [r for r in review_rows if any(x in r["missing_evidence"] for x in ("eligibility_evidence", "geo_opportunity_evidence", "usable_website_content", "industry", "geography", "employee_size_evidence"))],
            "phase4g_contact_channel_review.csv": [r for r in review_rows if r["contact_channel_review_status"] == "PENDING" and r["identity_verification_status"] == "VERIFIED" and r["eligibility"] == "ELIGIBLE"],
        }
        for name, rows in views.items():
            write_csv_atomic(output_dir / name, REVIEW_FIELDS, rows)
        write_csv_atomic(output_dir / "phase4g_source_diversity.csv", ["dimension", "source_group", "category", "company_count", "source_group_total", "percent_of_source_group"], diversity_rows)
        scale_fields = list(scales[0])
        write_csv_atomic(output_dir / "phase4g_scale_projection.csv", scale_fields, scales)
    return {"companies": len(review_rows), "diversity_rows": len(diversity_rows), "scale_rows": len(scales), **{name.removesuffix(".csv"): len(rows) for name, rows in views.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DB)
    parser.add_argument("--output-dir", type=Path, default=DATA)
    args = parser.parse_args()
    print(json.dumps(write_outputs(args.database, args.output_dir), sort_keys=True))


if __name__ == "__main__":
    main()
