"""Rebuild the Phase 4C.2 quality audit from saved Phase 4C.1 evidence only."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from outreach_agent.wikidata import diagnose_website_identity, wikidata_claim_values, write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAPSHOTS = DATA / "phase4c1_wikidata_snapshots"
AUDIT_FIELDS = [
    "qid", "company_name", "website_url", "normalized_domain", "original_status",
    "diagnostic_status", "verification_diagnosis", "http_status", "final_url", "page_title",
    "commercial_business_assessment", "business_class_qids", "industry_qids", "country_qids",
    "inception_dates", "dissolution_dates", "employee_values", "employee_size_classification",
    "size_evidence_note", "geo_relevance_assessment", "geo_evidence_note",
    "additional_evidence_needed", "human_review_required",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def entities_by_qid() -> dict[str, dict]:
    path = next(SNAPSHOTS.glob("entities-*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("entities", {})


def value_string(entity: dict, prop: str) -> str:
    return " | ".join(str(value).removeprefix("+") for value in wikidata_claim_values(entity, prop))


def employee_evidence(entity: dict) -> tuple[str, str]:
    values = wikidata_claim_values(entity, "P1128")
    clean: list[int] = []
    for value in values:
        try:
            clean.append(int(float(value)))
        except (TypeError, ValueError):
            pass
    # We report sourced large values as enterprise evidence without creating
    # a new eligibility threshold. No lower range is treated as SMB evidence.
    if any(value >= 10_000 for value in clean):
        return "LARGE_ENTERPRISE_SUPPORTED", f"P1128 values={values}; at least one published count is >=10,000"
    if values:
        return "SIZE_UNKNOWN", f"P1128 values={values}; no defensible SMB threshold is applied"
    return "SIZE_UNKNOWN", "No P1128 employee-count claim in saved entity snapshot"


def row_diagnosis(row: dict[str, str], verification: dict[str, str]) -> str:
    return diagnose_website_identity(
        row["company_name"], row["website_url"], verification["status"],
        http_status=verification["http_status"], final_url=verification["final_url"],
        page_title=verification["page_title"],
    )


def build_audit() -> list[dict[str, str]]:
    companies = read_csv(DATA / "phase4c1_wikidata_pilot.csv")
    verifications = {row["qid"]: row for row in read_csv(DATA / "phase4c1_website_verification.csv")}
    entities = entities_by_qid()
    audit: list[dict[str, str]] = []
    for company in companies:
        verification = verifications[company["qid"]]
        entity = entities.get(company["qid"], {})
        status = row_diagnosis(company, verification)
        size_class, size_note = employee_evidence(entity)
        name = company["company_name"]
        qid = company["qid"]
        if qid == "Q100105751":
            commercial, commercial_note = "NONCOMMERCIAL_INDICATED", "Name/title identify a foundation; business class alone does not prove commercial operation"
        elif qid == "Q950288":
            commercial, commercial_note = "ASSOCIATION_INDICATED", "Entity name identifies an association/trade body; commercial company status is unproven"
        else:
            commercial, commercial_note = "UNCONFIRMED", "Wikidata company class is not proof of current commercial operation"

        if status == "VERIFIED":
            diagnosis = "First-party title evidence retained; no mismatch shown in saved capture"
        elif status == "ACCESS_BLOCKED":
            diagnosis = "Access denied (403); identity remains unknown, not a mismatch"
        elif status == "FETCH_FAILED":
            diagnosis = f"Fetch failed ({verification['evidence']}); identity remains unknown, not a mismatch"
        elif status == "REJECTED_MISMATCH":
            diagnosis = "Unrelated gambling title and redirect to a different host"
        elif status == "INSUFFICIENT_EVIDENCE":
            diagnosis = "Saved title/domain evidence is too generic or label is non-distinctive"
        else:
            diagnosis = "Reachable content does not establish identity; possible alias, translation, or successor"

        geo = company["geo_opportunity"]
        if status != "VERIFIED":
            geo_assessment = "UNATTRIBUTABLE_UNVERIFIED_SITE"
            geo_note = f"Phase 4C.1 site score={geo}; identity not verified, so do not attribute it to this company"
        elif qid == "Q100105751":
            geo_assessment = "NOT_COMMERCIAL_GEO_TARGET"
            geo_note = "Website identity verified, but foundation/noncommercial status is indicated"
        else:
            geo_assessment = geo if geo in {"HIGH", "MEDIUM", "LOW"} else "UNKNOWN"
            geo_note = f"Saved site-content assessment={geo}; relevant only as a website-level signal, not proof of SMB fit"

        need = []
        if status in {"AMBIGUOUS_IDENTITY", "INSUFFICIENT_EVIDENCE"}:
            need.append("independent first-party identity evidence (legal name, about/imprint, or Wikidata-linked identifier)")
        elif status in {"FETCH_FAILED", "ACCESS_BLOCKED"}:
            need.append("independent accessible first-party identity source; do not bypass controls")
        if commercial == "UNCONFIRMED":
            need.append("current operating/commercial-status evidence")
        elif commercial in {"NONCOMMERCIAL_INDICATED", "ASSOCIATION_INDICATED"}:
            need.append("confirm whether commercial research scope includes this entity type")
        if size_class != "LARGE_ENTERPRISE_SUPPORTED":
            need.append("sourced employee count or other defensible size evidence")
        if not company["industry_qids"]:
            need.append("industry evidence")
        audit.append({
            "qid": qid, "company_name": name, "website_url": company["website_url"],
            "normalized_domain": company["normalized_domain"], "original_status": verification["status"],
            "diagnostic_status": status, "verification_diagnosis": diagnosis,
            "http_status": verification["http_status"], "final_url": verification["final_url"],
            "page_title": verification["page_title"], "commercial_business_assessment": commercial,
            "business_class_qids": value_string(entity, "P31"), "industry_qids": company["industry_qids"],
            "country_qids": company["country_qids"], "inception_dates": value_string(entity, "P571"),
            "dissolution_dates": value_string(entity, "P576"),
            "employee_values": company["employee_values"], "employee_size_classification": size_class,
            "size_evidence_note": size_note, "geo_relevance_assessment": geo_assessment,
            "geo_evidence_note": geo_note,
            "additional_evidence_needed": "; ".join(need), "human_review_required": "true",
        })
    return audit


def build_filters(audit: list[dict[str, str]]) -> list[dict[str, str]]:
    total = len(audit)
    verified = lambda rows: sum(row["diagnostic_status"] == "VERIFIED" for row in rows)
    rules = [
        ("Baseline cohort", lambda row: True, "Reference only; QID-order sample, not random"),
        ("Business organization type (P31 includes company)", lambda row: "Q4830453" in row["business_class_qids"], "No incremental filter: all 30 were selected by P31=company; other P31 types may be useful for review"),
        ("Industry evidence present (P452)", lambda row: bool(row["industry_qids"]), "20 records; completeness rises but does not establish business or GEO suitability"),
        ("Inception date present (P571)", lambda row: bool(row["inception_dates"]), "25 records; age is not a size proxy"),
        ("Country evidence present (P17)", lambda row: bool(row["country_qids"]), "26 records; missing geography must remain unknown"),
        ("Employee-count evidence present (P1128)", lambda row: bool(row["employee_values"]), "6 records; excludes 24 legitimate unknown-size candidates"),
        ("No explicit dissolution claim (P576)", lambda row: not bool(row["dissolution_dates"]), "27 retained; missing P576 does not prove operating status"),
        ("Official website claim present (P856)", lambda row: bool(row["website_url"]), "All 30; P856 is not verified identity"),
        ("Website identity verified", lambda row: row["diagnostic_status"] == "VERIFIED", "Outcome gate, not discovery filter; 5 verified after one false positive was downgraded"),
    ]
    results = []
    for label, predicate, interpretation in rules:
        kept = [row for row in audit if predicate(row)]
        results.append({
            "filter": label, "records_retained": str(len(kept)), "records_removed": str(total - len(kept)),
            "verified_websites_retained": str(verified(kept)),
            "verified_yield_among_retained_pct": f"{100 * verified(kept) / len(kept):.1f}" if kept else "0.0",
            "smb_supported_retained": "0", "human_review_required_retained": str(sum(row["human_review_required"] == "true" for row in kept)),
            "interpretation": interpretation,
        })
    return results


def main() -> None:
    audit = build_audit()
    write_csv_atomic(DATA / "phase4c2_company_quality_audit.csv", AUDIT_FIELDS, audit)
    filter_rows = build_filters(audit)
    write_csv_atomic(DATA / "phase4c2_filter_comparison.csv", list(filter_rows[0]), filter_rows)
    statuses = Counter(row["diagnostic_status"] for row in audit)
    print(json.dumps({"companies": len(audit), "statuses": statuses, "audit": "data/phase4c2_company_quality_audit.csv", "filters": "data/phase4c2_filter_comparison.csv"}, sort_keys=True))


if __name__ == "__main__":
    main()
