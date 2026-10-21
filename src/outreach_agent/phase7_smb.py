"""Offline SMB target reconciliation for the saved Phase 7 company pool."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from outreach_agent.classification import size_category
from outreach_agent.phase7 import _has_operational_evidence

ROOT = Path(__file__).resolve().parents[2]
PHASE7_DB = ROOT / "data/phase7_candidates.db"
ACTIVE_DB = ROOT / "data/phase6a_experimental.db"
CONTACTABLE_CSV = ROOT / "data/all_contactable_companies.csv"
CHANNEL_AUDIT_CSV = ROOT / "data/phase7_contact_channel_audit.csv"
OUT = ROOT / "data"
TARGET = 5000
HOTEL_DB = ROOT / "data/hotel_candidates.db"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [dict(row) for row in csv.DictReader(stream)]


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def normalize_company_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


def company_size_classification(value: Any, category: str | None, source: str | None) -> str:
    """Use only saved, source-reported size evidence and the project's existing category."""
    if value in (None, "", 0, "0") or not source or source.casefold() == "unknown":
        return "SIZE_UNKNOWN"
    try:
        employee_count = int(value)
    except (TypeError, ValueError):
        return "CONFLICTING_EVIDENCE"
    if employee_count <= 0:
        return "SIZE_UNKNOWN"
    stored = (category or "UNKNOWN").upper()
    if stored != size_category(employee_count):
        return "CONFLICTING_EVIDENCE"
    if stored == "LARGE":
        return "LARGE_ENTERPRISE_SUPPORTED"
    if stored in {"MICRO", "SMALL", "MEDIUM"}:
        return "SMB_SUPPORTED"
    return "SIZE_UNKNOWN"


def contact_channel_classification(has_email: bool, has_form: bool) -> str:
    if has_email and has_form:
        return "EMAIL_AND_FORM"
    if has_email:
        return "EMAIL_ONLY"
    if has_form:
        return "FORM_ONLY"
    return "NO_CONTACT"


def _hotel_target_record(industry: str) -> bool:
    tags = {part.strip().casefold() for part in (industry or "").split("|")}
    return "tourism=hotel" in tags or "amenity=hotel" in tags


def _sector(industry: str) -> str:
    text = (industry or "").casefold()
    if not text:
        return "Unknown"
    if any(token in text for token in ("tourism=hotel", "amenity=hotel", "tourism=guest_house", "tourism=motel")):
        return "Travel and lodging"
    if any(token in text for token in ("amenity=restaurant", "amenity=cafe", "amenity=fast_food", "amenity=bar", "amenity=pub", "craft=brewery", "shop=bakery")):
        return "Food and beverage"
    if any(token in text for token in ("amenity=dentist", "amenity=doctors", "amenity=clinic", "amenity=hospital", "healthcare=")):
        return "Healthcare"
    if any(token in text for token in ("office=lawyer", "office=accountant", "office=estate_agent", "office=insurance", "office=company")):
        return "Professional and business services"
    if "shop=" in text:
        return "Retail"
    if "office=educational" in text or "amenity=school" in text or "amenity=college" in text:
        return "Education"
    if "tourism=" in text:
        return "Arts, culture, and tourism"
    if "craft=" in text:
        return "Manufacturing and trades"
    return "Other / source category retained"


def _json_list(values: set[str]) -> str:
    return json.dumps(sorted(value for value in values if value), ensure_ascii=False, separators=(",", ":"))


def reconcile_rows(
    candidates: list[dict[str, Any]],
    active_by_domain: dict[str, dict[str, Any]],
    contactable_by_id: dict[str, dict[str, Any]],
    channels_by_id: dict[str, list[dict[str, Any]]],
    chain_concerns: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    name_groups: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        key = normalize_company_name(str(candidate.get("company_name") or ""))
        domain = str(candidate.get("canonical_domain") or "")
        location = str(candidate.get("location") or active_by_domain.get(domain, {}).get("location") or "")
        key = f"{key}|{normalize_company_name(location)}"
        if key:
            name_groups[key].add(str(candidate.get("candidate_id") or ""))

    out: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        domain = str(candidate.get("canonical_domain") or "")
        active = active_by_domain.get(domain, {})
        industry = str(candidate.get("industry") or active.get("industry") or "")
        location = str(candidate.get("location") or active.get("location") or "")
        size_value = active.get("company_size_value")
        size_category = str(active.get("company_size_category") or "UNKNOWN")
        size_source = str(active.get("company_size_source") or "unknown")
        size_class = company_size_classification(size_value, size_category, size_source)
        hotel = _hotel_target_record(industry)
        verification = str(candidate.get("verification_status") or "UNKNOWN")
        eligibility = str(candidate.get("geo_eligibility_status") or "UNKNOWN")
        operational = _has_operational_evidence(candidate.get("evidence_json"))
        normalized_name = normalize_company_name(str(candidate.get("company_name") or ""))
        identity_key = f"{normalized_name}|{normalize_company_name(location)}"
        name_collision = len(name_groups.get(identity_key, ())) > 1
        chain_flag = chain_concerns.get(candidate_id, "NO_STRUCTURED_SIGNAL")
        identity_review = name_collision or chain_flag not in {"NO_STRUCTURED_SIGNAL", "PRESERVED_EXISTING_COHORT", ""}

        contact = contactable_by_id.get(candidate_id, {})
        company_channels = channels_by_id.get(candidate_id, [])
        has_email = any(row.get("channel_classification") == "PUBLIC_BUSINESS_EMAIL" and row.get("email") for row in company_channels)
        has_form = any(row.get("channel_classification") == "OFFICIAL_CONTACT_FORM" and row.get("channel_url") for row in company_channels)
        channel_class = contact_channel_classification(has_email, has_form)

        verified_smb = (
            not hotel and size_class == "SMB_SUPPORTED" and verification == "VERIFIED"
            and operational and not identity_review
        )
        geo_smb = verified_smb and eligibility == "ELIGIBLE"
        contactable_smb = geo_smb and channel_class != "NO_CONTACT"

        emails = {str(row.get("email") or "").strip() for row in company_channels if row.get("channel_classification") == "PUBLIC_BUSINESS_EMAIL"}
        forms = {str(row.get("channel_url") or "").strip() for row in company_channels if row.get("channel_classification") == "OFFICIAL_CONTACT_FORM"}
        evidence = [{
            "classification": row.get("channel_classification", ""),
            "channel_url": row.get("channel_url", ""),
            "evidence_url": row.get("evidence_url", ""),
            "evidence": row.get("evidence", ""),
            "purpose": row.get("purpose", ""),
            "review_status": row.get("review_status", "PENDING"),
            "contact_source": row.get("contact_source", ""),
        } for row in company_channels]
        if hotel:
            target_status = "EXCLUDED_HOTEL_TARGET"
        elif identity_review:
            target_status = "NEEDS_REVIEW_COMPANY_IDENTITY"
        elif size_class == "LARGE_ENTERPRISE_SUPPORTED":
            target_status = "OUTSIDE_SMB_SIZE_SCOPE"
        elif size_class != "SMB_SUPPORTED":
            target_status = "NEEDS_REVIEW_SIZE"
        elif verification != "VERIFIED":
            target_status = "NEEDS_REVIEW_WEBSITE_IDENTITY"
        elif not operational:
            target_status = "NEEDS_REVIEW_OPERATIONAL_EVIDENCE"
        elif eligibility != "ELIGIBLE":
            target_status = "NEEDS_REVIEW_GEO_ELIGIBILITY"
        elif channel_class == "NO_CONTACT":
            target_status = "QUALIFIED_SMB_NO_USABLE_CONTACT"
        else:
            target_status = "QUALIFIED_CONTACTABLE_SMB"

        size_evidence = ""
        if size_value not in (None, ""):
            size_evidence = f"{size_value} employees; source={size_source}; saved_category={size_category}"
        if str(size_value) == "0":
            size_evidence += "; zero value retained as size unknown pending validation"
        source_ids = str(candidate.get("source_record_id") or "")
        out.append({
            "candidate_id": candidate_id,
            "company_name": candidate.get("company_name", ""),
            "website": candidate.get("website", ""),
            "canonical_domain": domain,
            "source_name": candidate.get("source_name", ""),
            "source_url": candidate.get("source_url", ""),
            "source_record_id": source_ids,
            "source_license_or_permission": candidate.get("source_license_or_permission", ""),
            "discovery_timestamp": candidate.get("discovery_timestamp", ""),
            "source_provenance_record_count": candidate.get("source_provenance_record_count", 0),
            "business_sector": _sector(industry),
            "industry_source_value": industry,
            "geography": location or "UNKNOWN",
            "verification_status": verification,
            "operational_evidence_status": "SUBSTANTIVE_FIRST_PARTY_PAGE_PRESENT" if operational else "NOT_ESTABLISHED",
            "geo_eligibility_status": eligibility,
            "geo_opportunity_status": candidate.get("geo_opportunity_status", "UNKNOWN"),
            "qualification_evidence": candidate.get("qualification_evidence", ""),
            "company_size_classification": size_class,
            "company_size_value": size_value if size_value not in (None, "") else "",
            "company_size_category": size_category,
            "company_size_source": size_source,
            "company_size_evidence": size_evidence,
            "identity_deduplication_status": "POTENTIAL_DUPLICATE_NAME_REVIEW" if name_collision else "SOURCE_IDENTITY_REVIEW" if identity_review else "UNIQUE_CANDIDATE_ID",
            "hotel_target_scope": "SEPARATE_HOTEL_TARGET" if hotel else "NOT_HOTEL_TAGGED",
            "smb_target_status": target_status,
            "verified_smb_candidate": "YES" if verified_smb else "NO",
            "geo_eligible_smb": "YES" if geo_smb else "NO",
            "contactable_smb": "YES" if contactable_smb else "NO",
            "contact_channel_classification": channel_class,
            "public_business_emails": _json_list(emails),
            "confirmed_contact_form_urls": _json_list(forms),
            "contact_evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "contact_review_status": ";".join(sorted({str(row.get("review_status") or "PENDING") for row in company_channels})) or "NO_CHANNEL_REVIEW_RECORD",
            "contactable_export_record": "YES" if contact else "NO",
            "outreach_authorization": "NOT_GRANTED",
        })

    out.sort(key=lambda row: row["candidate_id"])
    by_source = Counter(row["source_name"] for row in out)
    by_sector_all = Counter(row["business_sector"] for row in out)
    by_geography_all = Counter(row["geography"] for row in out)
    by_sector_contactable = Counter(row["business_sector"] for row in out if row["contactable_export_record"] == "YES")
    by_geography_contactable = Counter(row["geography"] for row in out if row["contactable_export_record"] == "YES")
    channels_all = Counter(row["contact_channel_classification"] for row in out)
    channels_contactable = Counter(row["contact_channel_classification"] for row in out if row["contactable_export_record"] == "YES")
    channels_smb = Counter(row["contact_channel_classification"] for row in out if row["contactable_smb"] == "YES")
    statuses = Counter(row["verification_status"] for row in out)
    geo_statuses = Counter(row["geo_eligibility_status"] for row in out)
    sizes = Counter(row["company_size_classification"] for row in out)
    summary = {
        "target": TARGET,
        "phase7_company_records": len(out),
        "unique_candidate_ids": len({row["candidate_id"] for row in out}),
        "unique_domains": len({row["canonical_domain"] for row in out}),
        "verified_website_identities": statuses.get("VERIFIED", 0),
        "geo_eligible_company_records": geo_statuses.get("ELIGIBLE", 0),
        "smb_size_supported_records": sizes.get("SMB_SUPPORTED", 0),
        "large_enterprise_supported_records": sizes.get("LARGE_ENTERPRISE_SUPPORTED", 0),
        "size_unknown_records": sizes.get("SIZE_UNKNOWN", 0),
        "conflicting_size_evidence_records": sizes.get("CONFLICTING_EVIDENCE", 0),
        "verified_smb_count": sum(row["verified_smb_candidate"] == "YES" for row in out),
        "geo_eligible_smb_count": sum(row["geo_eligible_smb"] == "YES" for row in out),
        "contactable_smb_count": sum(row["contactable_smb"] == "YES" for row in out),
        "remaining_gap_to_5000": max(0, TARGET - sum(row["contactable_smb"] == "YES" for row in out)),
        "contactable_unknown_size_nonhotel": sum(row["contactable_export_record"] == "YES" and row["hotel_target_scope"] != "SEPARATE_HOTEL_TARGET" and row["company_size_classification"] == "SIZE_UNKNOWN" for row in out),
        "contactable_identity_review_nonhotel": sum(row["contactable_export_record"] == "YES" and row["hotel_target_scope"] != "SEPARATE_HOTEL_TARGET" and row["smb_target_status"] == "NEEDS_REVIEW_COMPANY_IDENTITY" for row in out),
        "hotel_tagged_candidates_excluded": sum(row["hotel_target_scope"] == "SEPARATE_HOTEL_TARGET" for row in out),
        "hotel_tagged_contactable_excluded": sum(row["hotel_target_scope"] == "SEPARATE_HOTEL_TARGET" and row["contactable_export_record"] == "YES" for row in out),
        "strict_contactable_companies": sum(row["contactable_export_record"] == "YES" for row in out),
        "contact_channel_breakdown_all_candidates": {key: channels_all.get(key, 0) for key in ("EMAIL_ONLY", "FORM_ONLY", "EMAIL_AND_FORM", "NO_CONTACT")},
        "contact_channel_breakdown_contactable": {key: channels_contactable.get(key, 0) for key in ("EMAIL_ONLY", "FORM_ONLY", "EMAIL_AND_FORM", "NO_CONTACT")},
        "contact_channel_breakdown_contactable_smb": {key: channels_smb.get(key, 0) for key in ("EMAIL_ONLY", "FORM_ONLY", "EMAIL_AND_FORM", "NO_CONTACT")},
        "source_distribution": dict(sorted(by_source.items())),
        "sector_distribution": dict(sorted(by_sector_all.items())),
        "geography_distribution": dict(sorted(by_geography_all.items())),
        "contactable_sector_distribution": dict(sorted(by_sector_contactable.items())),
        "contactable_geography_distribution": dict(sorted(by_geography_contactable.items())),
        "identity_name_review_rows": sum(row["identity_deduplication_status"] == "POTENTIAL_DUPLICATE_NAME_REVIEW" for row in out),
    }
    return out, summary


def reconcile_phase7_smb(
    db_path: Path = PHASE7_DB,
    active_db: Path = ACTIVE_DB,
    contactable_path: Path = CONTACTABLE_CSV,
    channel_audit_path: Path = CHANNEL_AUDIT_CSV,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not db_path.exists():
        return [], {"phase7_company_records": 0, "verified_smb_count": 0, "geo_eligible_smb_count": 0, "contactable_smb_count": 0, "remaining_gap_to_5000": TARGET}
    with _readonly(db_path) as connection:
        candidates = [dict(row) for row in connection.execute("SELECT * FROM candidates ORDER BY candidate_id")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        chain_concerns = {}
        if "phase6e_screening" in tables:
            chain_concerns = {row[0]: row[1] for row in connection.execute("SELECT candidate_id,chain_location_concern FROM phase6e_screening")}
        provenance_counts: Counter[str] = Counter()
        if "candidate_provenance" in tables:
            provenance_counts.update({row[0]: row[1] for row in connection.execute("SELECT candidate_id,COUNT(*) FROM candidate_provenance GROUP BY candidate_id")})
        for candidate in candidates:
            candidate["source_provenance_record_count"] = provenance_counts.get(candidate["candidate_id"], 0)
    active_by_domain: dict[str, dict[str, Any]] = {}
    if active_db.exists():
        with _readonly(active_db) as connection:
            active_by_domain = {row["normalized_domain"]: dict(row) for row in connection.execute(
                "SELECT normalized_domain,company_size_value,company_size_category,company_size_source,industry,location FROM companies")}
    contactable_rows = _read_csv(contactable_path)
    contact_ids = [row.get("candidate_id", "") for row in contactable_rows]
    if len(contact_ids) != len(set(contact_ids)):
        raise ValueError("The saved Phase 7 strict-contactable export has duplicate company candidate IDs")
    candidate_ids = {str(row.get("candidate_id") or "") for row in candidates}
    if any(candidate_id not in candidate_ids for candidate_id in contact_ids):
        raise ValueError("The saved strict-contactable export contains a company ID absent from the Phase 7 database")
    contactable_by_id = {row.get("candidate_id", ""): row for row in contactable_rows}
    channels_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _read_csv(channel_audit_path):
        if row.get("channel_classification") in {"PUBLIC_BUSINESS_EMAIL", "OFFICIAL_CONTACT_FORM"}:
            channels_by_id[row.get("candidate_id", "")].append(row)
    return reconcile_rows(candidates, active_by_domain, contactable_by_id, channels_by_id, chain_concerns)


def hotel_domain_overlap(business_rows: list[dict[str, Any]], hotel_db: Path = HOTEL_DB) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Match cohort records by exact domain as review leads, never as identity proof."""
    empty = {"hotel_property_records_with_domains": 0, "hotel_property_domains": 0,
             "hotel_records_sharing_business_domain": 0, "potential_shared_domains": 0,
             "contactable_smb_records_with_hotel_domain": 0, "confirmed_cross_cohort_company_links": 0}
    if not hotel_db.exists():
        return [], empty
    business_by_domain = {str(row.get("canonical_domain") or ""): row for row in business_rows
                          if row.get("canonical_domain")}
    with _readonly(hotel_db) as connection:
        hotels = [dict(row) for row in connection.execute(
            "SELECT candidate_id,company_name,canonical_domain,verification_status,geo_eligibility_status,chain_name,chain_status "
            "FROM candidates WHERE canonical_domain<>'' ORDER BY candidate_id")]
    overlap = []
    for hotel in hotels:
        domain = str(hotel.get("canonical_domain") or "")
        business = business_by_domain.get(domain)
        if not business:
            continue
        overlap.append({
            "business_candidate_id": str(business.get("candidate_id") or ""),
            "business_name": str(business.get("company_name") or ""),
            "business_domain": domain,
            "business_smb_target_status": str(business.get("smb_target_status") or ""),
            "hotel_property_id": str(hotel.get("candidate_id") or ""),
            "hotel_property_name": str(hotel.get("company_name") or ""),
            "hotel_verification_status": str(hotel.get("verification_status") or ""),
            "hotel_geo_eligibility_status": str(hotel.get("geo_eligibility_status") or ""),
            "chain_name": str(hotel.get("chain_name") or ""),
            "chain_status": str(hotel.get("chain_status") or ""),
            "match_type": "EXACT_CANONICAL_DOMAIN_ONLY",
            "identity_review_status": "PENDING_NOT_CONFIRMED",
        })
    metrics = {
        "hotel_property_records_with_domains": len(hotels),
        "hotel_property_domains": len({str(row["canonical_domain"]) for row in hotels}),
        "hotel_records_sharing_business_domain": len(overlap),
        "potential_shared_domains": len({row["business_domain"] for row in overlap}),
        "contactable_smb_records_with_hotel_domain": len({row["business_candidate_id"] for row in overlap
            if row["business_smb_target_status"] == "QUALIFIED_CONTACTABLE_SMB"}),
        "confirmed_cross_cohort_company_links": 0,
    }
    return overlap, metrics


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            values = {}
            for field in fields:
                value = row.get(field, "")
                if isinstance(value, str) and value[:1] in "=+-@":
                    value = "'" + value
                values[field] = value
            writer.writerow(values)
    return str(path)


def export_reconciliation(output_dir: Path = OUT, *, db_path: Path = PHASE7_DB,
                          active_db: Path = ACTIVE_DB, contactable_path: Path = CONTACTABLE_CSV,
                          channel_audit_path: Path = CHANNEL_AUDIT_CSV,
                          hotel_db: Path = HOTEL_DB) -> dict[str, Any]:
    rows, summary = reconcile_phase7_smb(db_path, active_db, contactable_path, channel_audit_path)
    overlap, overlap_metrics = hotel_domain_overlap(rows, hotel_db)
    summary["hotel_cross_cohort_domain_overlap"] = overlap_metrics
    fields = list(rows[0]) if rows else ["candidate_id", "company_name", "canonical_domain"]
    contactable = [row for row in rows if row["contactable_export_record"] == "YES"]
    size_review = [row for row in contactable if row["hotel_target_scope"] != "SEPARATE_HOTEL_TARGET" and row["company_size_classification"] == "SIZE_UNKNOWN"]
    paths = {
        "reconciliation": _write_csv(output_dir / "phase7_smb_reconciliation.csv", rows, fields),
        "contactable_companies": _write_csv(output_dir / "phase7_smb_contactable_companies.csv", contactable, fields),
        "size_review_queue": _write_csv(output_dir / "phase7_smb_size_review_queue.csv", size_review, fields),
        "potential_hotel_domain_overlap": _write_csv(output_dir / "phase7_cohort_domain_overlap.csv", overlap,
            ["business_candidate_id", "business_name", "business_domain", "business_smb_target_status",
             "hotel_property_id", "hotel_property_name", "hotel_verification_status",
             "hotel_geo_eligibility_status", "chain_name", "chain_status", "match_type", "identity_review_status"]),
    }
    paths["summary"] = str(output_dir / "phase7_smb_reconciliation_summary.json")
    (output_dir / "phase7_smb_reconciliation_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"paths": paths, "summary": summary, "rows": rows}


def phase7_smb_summary() -> dict[str, Any]:
    """Return dashboard metrics computed from existing DB and saved Phase 7 evidence only."""
    rows, summary = reconcile_phase7_smb()
    _, summary["hotel_cross_cohort_domain_overlap"] = hotel_domain_overlap(rows)
    return summary


def write_reconciliation_report(summary: dict[str, Any], path: Path | None = None) -> Path:
    path = path or ROOT / "docs/phase7_smb_target_reconciliation.md"
    channels = summary.get("contact_channel_breakdown_contactable", {})
    smb_channels = summary.get("contact_channel_breakdown_contactable_smb", {})
    unknown_contactable = summary.get("contactable_unknown_size_nonhotel", 0)
    def table(values: dict[str, int], limit: int = 20) -> list[str]:
        return ["| Category | Companies |", "|---|---:|", *[f"| {key} | {value:,} |" for key, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:limit]]]
    lines = [
        "# Phase 7 SMB/SME target reconciliation", "",
        "Snapshot generated from saved local Phase 7 company/contact evidence and read-only hotel-property data. No discovery, crawling, or database mutation was performed.", "",
        "## Reconciled counts", "",
        f"- Existing Phase 7 company records: **{summary['phase7_company_records']:,}** ({summary['unique_candidate_ids']:,} stable company candidate IDs; {summary['unique_domains']:,} canonical domains).",
        f"- Website identity verified: **{summary['verified_website_identities']:,}**; Phase 7 GEO eligible: **{summary['geo_eligible_company_records']:,}** across all sizes.",
        f"- Saved positive SMB size evidence: **{summary['smb_size_supported_records']:,}** records; `SIZE_UNKNOWN`: **{summary['size_unknown_records']:,}**; large-enterprise evidence: **{summary['large_enterprise_supported_records']:,}**; conflicting size records: **{summary['conflicting_size_evidence_records']:,}**.",
        f"- Size-supported SMB candidates with verified identity and a substantive first-party website signal: **{summary['verified_smb_count']:,}**.",
        f"- Of these, existing GEO qualification marks **{summary['geo_eligible_smb_count']:,}** eligible.",
        f"- Strict contactable, GEO-eligible SMBs: **{summary['contactable_smb_count']:,}**. Gap to 5,000: **{summary['remaining_gap_to_5000']:,}**.",
        f"- Strict contactable, non-hotel records with unknown size: **{unknown_contactable:,}**. These remain candidates for review, not automatic exclusions.",
        f"- Hotel-tagged candidates in the company pool: **{summary['hotel_tagged_candidates_excluded']:,}**; strict-contactable hotel-tagged records: **{summary['hotel_tagged_contactable_excluded']:,}**. They are excluded from the 5,000 SMB count and remain in the separate 3,000-hotel target/workflow.",
        f"- Separate hotel property pool: **{summary['hotel_cross_cohort_domain_overlap']['hotel_property_records_with_domains']:,}** properties with websites; **{summary['hotel_cross_cohort_domain_overlap']['hotel_records_sharing_business_domain']:,}** property records share an exact domain with **{summary['hotel_cross_cohort_domain_overlap']['potential_shared_domains']:,}** general-business domains, including **{summary['hotel_cross_cohort_domain_overlap']['contactable_smb_records_with_hotel_domain']:,}** contactable SMB-candidate records. These are potential domain overlaps only; confirmed cross-cohort company links: **{summary['hotel_cross_cohort_domain_overlap']['confirmed_cross_cohort_company_links']:,}**. Chain sites can serve many properties, so no records are merged or double-counted on domain alone.",
        "",
        "## Company-size policy", "",
        "`SMB_SUPPORTED` requires a positive, source-reported employee count and a consistent saved `MICRO`, `SMALL`, or `MEDIUM` category from the existing project classification. The reconciliation adds no employee or revenue cutoff. This is an evidence label, not SBA certification. `LARGE_ENTERPRISE_SUPPORTED` uses the existing saved `LARGE` category. Missing, zero, or unsourced values remain `SIZE_UNKNOWN`; inconsistent saved counts/categories are `CONFLICTING_EVIDENCE`. No count or revenue is inferred from sector, website appearance, age, source affiliation, or geography.",
        "",
        "## Contact channels", "",
        "The classifications below count companies, not channel rows. A company with both methods appears once as `EMAIL_AND_FORM`. `NO_CONTACT` means the current strict Phase 7 export has no qualifying public business email or confirmed first-party form; lower-quality, contact-page-only, rejected, or unverified evidence may still exist.",
        "",
        "### Full 57,664-record candidate pool", "",
        "| Channel class | Companies |", "|---|---:|",
        *[f"| {key} | {summary['contact_channel_breakdown_all_candidates'].get(key, 0):,} |" for key in ("EMAIL_ONLY", "FORM_ONLY", "EMAIL_AND_FORM", "NO_CONTACT")],
        "",
        f"### All {summary['strict_contactable_companies']:,} strict contactable companies", "",
        "| Channel class | Companies |", "|---|---:|",
        *[f"| {key} | {channels.get(key, 0):,} |" for key in ("EMAIL_ONLY", "FORM_ONLY", "EMAIL_AND_FORM", "NO_CONTACT")],
        "",
        "### SMBs that meet the saved size, identity, GEO, and contact criteria", "",
        "| Channel class | Companies |", "|---|---:|",
        *[f"| {key} | {smb_channels.get(key, 0):,} |" for key in ("EMAIL_ONLY", "FORM_ONLY", "EMAIL_AND_FORM", "NO_CONTACT")],
        "",
        "## Source, sector, and geography", "",
        "Source composition of the full Phase 7 company records:", "",
        *table(summary["source_distribution"]), "",
        "Sector distribution of strict contactable records (raw source categories are retained in the CSV):", "",
        *table(summary["contactable_sector_distribution"]), "",
        "Most common saved locations among strict contactable records:", "",
        *table(summary["contactable_geography_distribution"], 25), "",
        f"Potential exact-name collision rows across different candidate IDs: **{summary['identity_name_review_rows']:,}**. They remain separate candidate records and are not merged by name; identity review is required before counting any unresolved candidate as a distinct company.",
        "",
        "## Interpretation and limitations", "",
        f"The 7,172 strict-contactable total is not an SMB total. The saved Phase 7 schema has no employee-size evidence for the OSM records. The only usable saved size evidence comes from the original company-size fields and is joined by canonical domain to the Phase 7 stable candidate ID. The current evidence supports {summary['verified_smb_count']:,} verified, operating-signal, size-supported SMB candidates and {summary['geo_eligible_smb_count']:,} GEO-eligible SMBs; {summary['contactable_smb_count']:,} also meet the strict contactability criteria. A further {summary.get('contactable_identity_review_nonhotel', 0):,} contactable record(s) with reported SMB size have unresolved same-name/location identity collisions. Other contactable businesses stay `SIZE_UNKNOWN` and need sourced size evidence or human review. This does not establish that all unknown-size firms are ineligible.",
        "",
        "The operating signal is the existing bounded first-party website check: identity status `VERIFIED` plus a saved successful page with substantive visible text. It is not proof of business registration or current transactions. Counts use stable Phase 7 candidate IDs, not channel rows. Existing company-size evidence is matched to those IDs by canonical domain; exact normalized name/location collisions are flagged and excluded from the count pending identity review, not automatically merged. The Phase 7 source schema enforces one candidate per domain, so unobserved alias-domain identities may still need human reconciliation. The Northeast-heavy OSM pool and its business-category mix are not representative of the broader SMB/SME population.",
        "",
        "Contact email/form evidence, evidence excerpts, source URLs, and review states are retained in the contactable-company exports. The reconciliation includes each candidate's primary source URL/record ID/license and the count of source provenance records; the complete provenance table remains intact in the source database. Existing channel-review decisions remain unchanged; the saved contact channels remain pending unless a prior human decision says otherwise. No outreach authorization is created.",
        "",
        "## Reproducibility", "",
        "Run `PYTHONPATH=src uv run python scripts/phase7_smb_reconcile.py` to regenerate the reconciliation CSVs and this report from the saved Phase 7 database, the preserved 537-company database, current hotel-property data, and existing Phase 7 exports. The command opens databases read-only and does not issue website requests.",
        "",
        "### Export paths", "",
        "- `data/phase7_smb_reconciliation.csv` (all Phase 7 candidate records)",
        "- `data/phase7_smb_contactable_companies.csv` (the 7,172 strict-contactable records with SMB status)",
        "- `data/phase7_smb_size_review_queue.csv` (non-hotel strict-contactable candidates with unknown size)",
        "- `data/phase7_cohort_domain_overlap.csv` (unconfirmed exact-domain review leads across separate pools)",
        "- `data/phase7_smb_reconciliation_summary.json`", "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
