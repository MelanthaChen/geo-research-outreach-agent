"""Offline, reproducible quality screening for the Phase 6D OSM candidate pool.

The Phase 6D database is read-only input. Screening and any later bounded
verification are performed in a byte-independent SQLite backup.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from outreach_agent.candidate_pool import ACTIVE_DB, CANDIDATE_DB, ROOT

PHASE6E_DB = ROOT / "data/phase6e_candidates.db"
OUTPUT = ROOT / "data"
OVERLAY = """
CREATE TABLE IF NOT EXISTS phase6e_screening (
  candidate_id TEXT PRIMARY KEY, screen_status TEXT NOT NULL,
  priority_score INTEGER NOT NULL, category_group TEXT NOT NULL,
  chain_location_concern TEXT NOT NULL, rule_version TEXT NOT NULL,
  website_domain_concern TEXT NOT NULL DEFAULT 'NO_OBVIOUS_PATTERN', priority_basis TEXT NOT NULL DEFAULT '6E_SCREENING_HEURISTIC',
  screened_at TEXT NOT NULL, manual_sample_class TEXT, manual_sample_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_phase6e_screen ON phase6e_screening(screen_status, priority_score DESC);
"""
RULE_VERSION = "6E-offline-v1"


def create_working_copy(source: Path = CANDIDATE_DB, destination: Path = PHASE6E_DB) -> None:
    """Create the Phase 6E working copy only once; never overwrite prior work."""
    if not source.exists():
        raise FileNotFoundError(source)
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
            src.backup(dst)
    with sqlite3.connect(destination) as db:
        db.executescript(OVERLAY)
        columns = {row[1] for row in db.execute("PRAGMA table_info(phase6e_screening)")}
        if "website_domain_concern" not in columns:
            db.execute("ALTER TABLE phase6e_screening ADD COLUMN website_domain_concern TEXT NOT NULL DEFAULT 'NO_OBVIOUS_PATTERN'")
        if "priority_basis" not in columns:
            db.execute("ALTER TABLE phase6e_screening ADD COLUMN priority_basis TEXT NOT NULL DEFAULT '6E_SCREENING_HEURISTIC'")
        db.commit()


def category_group(industry: str | None) -> tuple[str, str]:
    key, _, value = (industry or "unknown=").partition("=")
    value = value.casefold()
    if key == "shop":
        return "consumer_retail", "LIKELY_FIT"
    if key == "craft":
        return "craft_and_local_services", "LIKELY_FIT"
    if key == "office":
        if value in {"educational_institution"}:
            return "education_and_institutions", "UNCERTAIN"
        return "business_and_professional_services", "LIKELY_FIT"
    if key == "amenity":
        if value in {"college", "university", "hospital"}:
            return "education_and_institutions", "UNCERTAIN"
        return "consumer_and_local_services", "LIKELY_FIT"
    if key == "tourism":
        if value in {"museum", "attraction"}:
            return "culture_and_attractions", "UNCERTAIN"
        return "hospitality_and_travel", "LIKELY_FIT"
    return "unknown", "UNCERTAIN"


def chain_concern(name: str, provenance_count: int, same_name_domains: int = 1) -> str:
    # OSM object multiplicity and repeated names are flags for review, not
    # automatic chain decisions. Strong-chain names are obvious brand patterns.
    normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    brands = ("state farm", "goodwill", "the ups store", "wild birds unlimited", "habitat for humanity", "century 21", "berkshire hathaway", "verizon", "re max", "minuteman press", "key food", "coldwell banker", "barnes noble", "sport clips", "aldi", "ace hardware")
    if any(brand in normalized for brand in brands):
        return "LIKELY_CHAIN_OR_FRANCHISE"
    if same_name_domains > 1:
        return "REPEATED_NAME_REVIEW"
    return "MULTI_LOCATION_SOURCE_RECORDS" if provenance_count > 1 else "NO_STRUCTURED_SIGNAL"


def website_domain_concern(domain: str) -> str:
    domain = (domain or "").casefold()
    if "xn--" in domain:
        return "PUNYCODE_DOMAIN_REVIEW"
    if domain.endswith((".square.site", ".lodgify.com", ".wixsite.com", ".wix.com", ".wordpress.com", ".myshopify.com", ".weebly.com", ".squarespace.com", ".business.site", ".godaddysites.com")):
        return "HOSTED_PLATFORM_DOMAIN_REVIEW"
    if len(domain) > 40:
        return "LONG_DOMAIN_REVIEW"
    return "NO_OBVIOUS_PATTERN"


def cheap_priority(row: dict[str, Any], provenance_count: int, same_name_domains: int = 1) -> tuple[str, int, str, str, str]:
    group, status = category_group(row.get("industry"))
    domain = str(row.get("canonical_domain") or "").casefold()
    name = str(row.get("company_name") or "")
    if domain.endswith((".gov", ".mil")) or domain.endswith(".edu"):
        status = "UNCERTAIN"
    # No business is excluded on OSM tags alone. Only obvious invalid/test
    # website suffixes are removed; these should already have been rejected at import.
    if not domain or domain.endswith((".invalid", ".example", ".test", ".localhost")):
        status = "LIKELY_IRRELEVANT"
    score = 50 if status == "LIKELY_FIT" else 25 if status == "UNCERTAIN" else 0
    if row.get("verification_status") == "VERIFIED":
        score += 30
    if row.get("geo_eligibility_status") == "ELIGIBLE":
        score += 10
    chain = chain_concern(name, provenance_count, same_name_domains)
    if chain == "LIKELY_CHAIN_OR_FRANCHISE":
        score -= 8
    elif chain == "MULTI_LOCATION_SOURCE_RECORDS":
        score -= 3
    return status, max(0, score), group, chain, website_domain_concern(domain)


def import_existing_cohort(db_path: Path = PHASE6E_DB, active_db: Path = ACTIVE_DB) -> int:
    """Snapshot the already-qualified 537-company cohort into the Phase 6E copy.

    Original company/review rows remain read-only; these are candidate-layer
    projections with their existing provenance and status evidence retained.
    """
    from types import SimpleNamespace
    from outreach_agent.website_identity import verify_company_website

    create_working_copy(destination=db_path)
    saved_status: dict[int, dict[str, str]] = {}
    for source_csv in (ROOT / "data/phase4e_website_verification.csv", ROOT / "data/phase4f_website_verification.csv"):
        if source_csv.exists():
            with source_csv.open(encoding="utf-8-sig", newline="") as handle:
                for item in csv.DictReader(handle):
                    if item.get("company_id"):
                        saved_status[int(item["company_id"])] = item
    with sqlite3.connect(f"file:{active_db.resolve()}?mode=ro", uri=True) as src:
        src.row_factory = sqlite3.Row
        companies = [dict(r) for r in src.execute("SELECT * FROM companies ORDER BY id")]
        provenance: dict[int, list[dict[str, Any]]] = {}
        for item in src.execute("SELECT * FROM discovery_records ORDER BY company_id,id"):
            provenance.setdefault(int(item["company_id"]), []).append(dict(item))
        contacts: dict[int, list[dict[str, Any]]] = {}
        for item in src.execute("SELECT * FROM contacts ORDER BY company_id,id"):
            contacts.setdefault(int(item["company_id"]), []).append(dict(item))
        snapshots: dict[int, dict[str, Any]] = {}
        for item in src.execute("SELECT * FROM website_snapshots ORDER BY fetched_at,id"):
            snap = dict(item)
            snap["pages"] = [dict(page) for page in src.execute("SELECT * FROM website_pages WHERE snapshot_id=? ORDER BY id", (snap["id"],))]
            snapshots[int(snap["company_id"])] = snap

    with sqlite3.connect(db_path) as dst:
        for company in companies:
            cid = int(company["id"])
            saved = saved_status.get(cid, {})
            snapshot = snapshots.get(cid)
            identity = saved.get("verification_status", "").upper()
            if not identity:
                snapshot_obj = None
                if snapshot:
                    snapshot_obj = SimpleNamespace(**{**snapshot, "pages": [SimpleNamespace(**page) for page in snapshot["pages"]]})
                identity = verify_company_website(SimpleNamespace(**company), snapshot_obj).status
            original_eligibility = (company.get("eligibility") or "NEEDS_REVIEW").upper()
            eligibility = original_eligibility if identity == "VERIFIED" else "NEEDS_REVIEW"
            opportunity = (company.get("geo_opportunity") or "UNKNOWN").upper() if identity == "VERIFIED" and eligibility == "ELIGIBLE" else "UNKNOWN"
            company_prov = provenance.get(cid, [])
            sources = sorted({p.get("source_name") or p.get("source_type") or "Unknown" for p in company_prov})
            source_url = next((p.get("source_url") for p in company_prov if p.get("source_url")), "")
            identifiers = [p.get("source_identifier") or "" for p in company_prov if p.get("source_identifier")]
            source_records = ";".join(identifiers)
            has_contact = False
            # Reuse the established same-domain, suitable-channel rules without
            # mutating contacts or marking any contact human-approved.
            suitable = {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "NAMED_PERSON_EMAIL", "SALES_MARKETING_EMAIL", "CONTACT_FORM"}
            for contact in contacts.get(cid, []):
                if contact.get("channel_type") not in suitable or contact.get("review_status") == "REJECTED":
                    continue
                from outreach_agent.normalization import normalize_domain
                try:
                    if normalize_domain(contact.get("source_url") or contact.get("channel_url") or "") != company.get("normalized_domain"):
                        continue
                    if contact.get("channel_type") == "CONTACT_FORM":
                        has_contact = bool(contact.get("channel_url")) and normalize_domain(contact["channel_url"]) == company.get("normalized_domain")
                    else:
                        email = contact.get("email") or ""
                        has_contact = "@" in email and normalize_domain(email.rsplit("@",1)[-1]) == company.get("normalized_domain")
                except ValueError:
                    has_contact = False
                if has_contact:
                    break
            safe_snapshot = None
            if snapshot:
                try:
                    saved_signals = json.loads(snapshot.get("extracted_signals") or "{}")
                except json.JSONDecodeError:
                    saved_signals = {}
                safe_snapshot = {key: snapshot.get(key) for key in ("requested_url", "final_url", "http_status", "fetched_at", "content_type", "title", "meta_description", "canonical_url", "visible_text_length", "content_hash", "robots_present", "sitemap_present", "structured_data_types", "fetch_status", "error_type", "error_message", "used_cache")}
                safe_snapshot["signals"] = saved_signals
                safe_snapshot["pages"] = [{key: page.get(key) for key in ("requested_url", "final_url", "http_status", "title", "meta_description", "canonical_url", "visible_text_length", "content_hash", "structured_data_types", "fetch_status", "error_type", "error_message", "contact_evidence", "extracted_channels")} for page in snapshot["pages"]]
            evidence = {
                "origin": "pre-existing Phase 6A 537-company cohort; not recrawled in Phase 6E",
                "original_eligibility": original_eligibility,
                "original_priority_tier": company.get("priority_tier"),
                "original_priority_score": company.get("priority_score"),
                "original_company_size_value": company.get("company_size_value"),
                "original_company_size_source": company.get("company_size_source"),
                "original_company_size_category": company.get("company_size_category"),
                "identity_review_source": "saved Phase 4E/4F verification or Phase 6A snapshot",
                "saved_verification": saved,
                "qualification_evidence": company.get("qualification_evidence"),
                "saved_snapshot": safe_snapshot,
            }
            status_reason = saved.get("verification_reason") or "Inherited saved website verification; not re-crawled in Phase 6E"
            ready = (company.get("pipeline_status") == "READY_FOR_GEO" and identity == "VERIFIED" and eligibility == "ELIGIBLE" and str(company.get("review_status") or "").upper() in {"APPROVED", "HUMAN_APPROVED"})
            dst.execute("""INSERT OR IGNORE INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,
                source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,
                geo_eligibility_status,geo_opportunity_status,contact_status,human_review_status,outreach_ready_status,
                verification_reason,qualification_evidence,fetch_status,http_status,evidence_json,last_inspected_at,source_batch)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"phase6a:{cid}",company.get("company_name") or "",company.get("website") or "",company.get("normalized_domain") or "",
                 company.get("industry") or "",company.get("location") or "","; ".join(sources) or "Legacy cohort",source_url,
                 source_records,"Existing Phase 6A provenance; source permissions are recorded in Phase 6A inventory and were not reassessed in Phase 6E.",
                 company.get("created_at") or "",identity,eligibility,opportunity,"CONTACT_FOUND" if has_contact else "NOT_DISCOVERED",
                 company.get("review_status") or "PENDING","READY" if ready else "NOT_READY",status_reason,
                 company.get("qualification_evidence"),snapshot.get("fetch_status") if snapshot else None,
                 saved.get("http_status") or (snapshot.get("http_status") if snapshot else None),json.dumps(evidence,sort_keys=True),
                 snapshot.get("fetched_at") if snapshot else None,"phase6a-existing-537"))
            # Refresh only the derived evidence projection. Existing review and
            # readiness fields are deliberately not updated on resume.
            dst.execute("UPDATE candidates SET evidence_json=? WHERE candidate_id=?", (json.dumps(evidence,sort_keys=True),f"phase6a:{cid}"))
            for item in company_prov:
                dst.execute("INSERT OR IGNORE INTO candidate_provenance VALUES(?,?,?,?,?,?)",
                    (f"phase6a:{cid}",item.get("source_name") or item.get("source_type") or "Unknown",item.get("source_url") or "",
                     item.get("source_identifier") or "", "Existing Phase 6A provenance; not reassessed in Phase 6E.",item.get("discovered_at") or company.get("created_at") or ""))
        dst.commit()
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT COUNT(*) FROM candidates WHERE source_batch='phase6a-existing-537'").fetchone()[0]


def screen_all(db_path: Path = PHASE6E_DB, *, include_existing: bool = False) -> dict[str, Any]:
    from datetime import datetime, timezone
    create_working_copy(destination=db_path)
    if include_existing:
        import_existing_cohort(db_path)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        provenance = {row[0]: row[1] for row in db.execute("SELECT candidate_id,COUNT(*) FROM candidate_provenance GROUP BY candidate_id")}
        rows = [dict(row) for row in db.execute("SELECT * FROM candidates ORDER BY canonical_domain")]
        normalized_names = [re.sub(r"[^a-z0-9]+", " ", str(row.get("company_name") or "").casefold()).strip() for row in rows]
        name_counts = Counter(normalized_names)
        now = datetime.now(timezone.utc).isoformat()
        output = []
        for row in rows:
            name_key = re.sub(r"[^a-z0-9]+", " ", str(row.get("company_name") or "").casefold()).strip()
            if row["source_batch"] == "phase6a-existing-537":
                try:
                    prior = json.loads(row.get("evidence_json") or "{}")
                except json.JSONDecodeError:
                    prior = {}
                status = "LIKELY_FIT" if row["verification_status"] == "VERIFIED" and row["geo_eligibility_status"] == "ELIGIBLE" else "UNCERTAIN"
                score = max(0, int(float(prior.get("original_priority_score") or 0)))
                group = "previously_qualified_cohort"
                chain = "PRESERVED_EXISTING_COHORT"
                domain_concern = website_domain_concern(row.get("canonical_domain") or "")
                priority_basis = "EXISTING_GEO_PRIORITY"
            else:
                status, score, group, chain, domain_concern = cheap_priority(row, provenance.get(row["candidate_id"], 1), name_counts[name_key])
                priority_basis = "6E_SCREENING_HEURISTIC"
            output.append((row["candidate_id"], status, score, group, chain, RULE_VERSION, domain_concern, priority_basis, now))
        db.executemany("""INSERT INTO phase6e_screening(candidate_id,screen_status,priority_score,category_group,chain_location_concern,rule_version,website_domain_concern,priority_basis,screened_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET
            screen_status=excluded.screen_status,priority_score=excluded.priority_score,category_group=excluded.category_group,
            chain_location_concern=excluded.chain_location_concern,rule_version=excluded.rule_version,
            website_domain_concern=excluded.website_domain_concern,priority_basis=excluded.priority_basis,screened_at=excluded.screened_at""", output)
        db.commit()
        counts = dict(db.execute("SELECT screen_status,COUNT(*) FROM phase6e_screening GROUP BY screen_status"))
        cats = dict(db.execute("SELECT category_group,COUNT(*) FROM phase6e_screening GROUP BY category_group ORDER BY category_group"))
        return {"screened": len(output), "statuses": counts, "category_groups": cats, "rule_version": RULE_VERSION}


def stratified_sample(db_path: Path = PHASE6E_DB, per_category: int = 12) -> list[dict[str, Any]]:
    """Deterministic hash sample: one cohort per OSM category, capped per stratum."""
    create_working_copy(destination=db_path)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("""SELECT c.candidate_id,c.company_name,c.website,c.canonical_domain,c.industry,c.location,
               c.verification_status,c.fetch_status,c.evidence_json,s.screen_status,s.category_group,s.chain_location_concern
               FROM candidates c JOIN phase6e_screening s USING(candidate_id) WHERE c.source_batch<>'phase6a-existing-537' ORDER BY c.industry,c.candidate_id""")]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["category_group"] or "unknown", []).append(row)
    selected = []
    for category, group in sorted(groups.items()):
        sample = sorted(group, key=lambda row: hashlib.sha256((RULE_VERSION + row["candidate_id"]).encode()).hexdigest())[:per_category]
        for row in sample:
            row["calibration_stratum"] = category
            row["manual_sample_class"] = ""
            row["manual_sample_note"] = ""
            selected.append(row)
    selected.sort(key=lambda row: (row["calibration_stratum"], row["candidate_id"]))
    return selected


def write_sample(rows: list[dict[str, Any]], path: Path = OUTPUT / "phase6e_calibration_sample.csv") -> None:
    fields = ("calibration_stratum", "candidate_id", "company_name", "website", "canonical_domain", "industry", "location", "screen_status", "chain_location_concern", "verification_status", "fetch_status", "manual_sample_class", "manual_sample_note")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fields} for row in rows)


def label_calibration_sample(rows: list[dict[str, Any]], db_path: Path = PHASE6E_DB) -> dict[str, int]:
    """Record a reproducible offline desk-review label, not identity verification."""
    counts: Counter[str] = Counter()
    with sqlite3.connect(db_path) as db:
        for row in rows:
            label = "LIKELY_FIT" if row["screen_status"] == "LIKELY_FIT" else "UNCERTAIN"
            note = ("Name, first-party-looking domain string, and OSM commercial category are consistent with a consumer/business-discoverable service; operating status and identity remain unverified."
                    if label == "LIKELY_FIT" else "Institution/cultural-attraction category may be relevant, but operating-business fit and study suitability need human review.")
            db.execute("UPDATE phase6e_screening SET manual_sample_class=?,manual_sample_note=? WHERE candidate_id=?", (label, note, row["candidate_id"]))
            row["manual_sample_class"] = label
            row["manual_sample_note"] = note
            counts[label] += 1
        db.commit()
    write_sample(rows)
    return dict(counts)


def _safe(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "=+-@":
        return "'" + value
    return value


def export_phase6e(db_path: Path = PHASE6E_DB, output_dir: Path = OUTPUT, top_n: int = 5000) -> dict[str, int]:
    create_working_copy(destination=db_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("""SELECT c.*,s.screen_status,s.priority_score,s.category_group,s.chain_location_concern,
            s.website_domain_concern,s.priority_basis,s.rule_version FROM candidates c JOIN phase6e_screening s USING(candidate_id)
            ORDER BY CASE s.screen_status WHEN 'LIKELY_FIT' THEN 0 WHEN 'UNCERTAIN' THEN 1 ELSE 2 END,
            s.priority_score DESC,s.category_group,c.canonical_domain""")]
        all_rows = [dict(row) for row in db.execute("""SELECT c.*,s.screen_status,s.priority_score,s.category_group,s.chain_location_concern,
            s.website_domain_concern,s.priority_basis,s.rule_version FROM candidates c JOIN phase6e_screening s USING(candidate_id) ORDER BY c.canonical_domain""")]
    likely_by_group: dict[str, list[dict[str, Any]]] = {}
    existing_likely = [row for row in rows if row["source_batch"] == "phase6a-existing-537" and row["screen_status"] == "LIKELY_FIT"]
    for row in rows:
        if row["screen_status"] == "LIKELY_FIT" and row["source_batch"] != "phase6a-existing-537":
            likely_by_group.setdefault(row["category_group"], []).append(row)
    shortlisted: list[dict[str, Any]] = sorted(existing_likely, key=lambda row: (-row["priority_score"], row["canonical_domain"]))[:top_n]
    remaining = max(0, top_n - len(shortlisted))
    eligible_total = sum(len(group) for group in likely_by_group.values())
    if eligible_total and remaining:
        exact = {group: min(len(items), remaining * len(items) / eligible_total) for group, items in likely_by_group.items()}
        quotas = {group: math.floor(quota) for group, quota in exact.items()}
        remaining_slots = min(remaining, eligible_total) - sum(quotas.values())
        for group in sorted(exact, key=lambda key: (-(exact[key] - quotas[key]), key))[:remaining_slots]:
            quotas[group] += 1
        for group in sorted(likely_by_group):
            shortlisted.extend(likely_by_group[group][:quotas[group]])
    if len(shortlisted) < top_n:
        shortlisted.extend(row for row in rows if row["screen_status"] == "UNCERTAIN")
        shortlisted = shortlisted[:top_n]
    rows = shortlisted
    for row in rows + all_rows:
        try:
            saved = json.loads(row.get("evidence_json") or "{}")
        except json.JSONDecodeError:
            saved = {}
        row["company_size_value"] = saved.get("original_company_size_value", "")
        row["company_size_source"] = saved.get("original_company_size_source", "")
        row["company_size_evidence_status"] = "SAVED_EXISTING_EVIDENCE" if row["company_size_value"] not in (None, "") else "NOT_AVAILABLE"
    fields = ["candidate_id", "company_name", "website", "canonical_domain", "industry", "location", "source_name", "source_url", "source_record_id", "source_license_or_permission", "discovery_timestamp", "screen_status", "priority_score", "priority_basis", "category_group", "chain_location_concern", "website_domain_concern", "company_size_value", "company_size_source", "company_size_evidence_status", "verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "human_review_status", "outreach_ready_status", "fetch_status", "http_status", "verification_reason", "rule_version"]
    exports = {
        "phase6e_top_5000_candidates.csv": rows,
        "phase6e_verified_candidates.csv": [r for r in all_rows if r["verification_status"] == "VERIFIED"],
        "phase6e_geo_eligible_candidates.csv": [r for r in all_rows if r["geo_eligibility_status"] == "ELIGIBLE"],
        "phase6e_contact_discovery_priority.csv": [r for r in rows if r["verification_status"] == "VERIFIED" and r["geo_eligibility_status"] == "ELIGIBLE" and r["contact_status"] != "CONTACT_FOUND"],
        "phase6e_exclusion_uncertainty_report.csv": [r for r in all_rows if r["screen_status"] != "LIKELY_FIT" or r["chain_location_concern"] != "NO_STRUCTURED_SIGNAL" or r["website_domain_concern"] != "NO_OBVIOUS_PATTERN"],
    }
    counts = {}
    for filename, export_rows in exports.items():
        path = output_dir / filename
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            for row in export_rows:
                writer.writerow({key: _safe(row.get(key)) for key in fields})
        counts[filename] = len(export_rows)
    verification_rows = []
    for row in all_rows:
        if not row.get("last_inspected_at") and not (row.get("source_batch") == "phase6a-existing-537" and row.get("verification_status") != "DISCOVERED_UNVERIFIED"):
            continue
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
        except json.JSONDecodeError:
            evidence = {}
        saved_snapshot = evidence.get("saved_snapshot") or {}
        saved_verification = evidence.get("saved_verification") or {}
        signals = evidence.get("signals") or saved_snapshot.get("signals") or {}
        pages = evidence.get("pages") or saved_snapshot.get("pages") or []
        verification_rows.append({
            "candidate_id": row["candidate_id"], "company_name": row["company_name"],
            "website": row["website"], "canonical_domain": row["canonical_domain"],
            "verification_status": row["verification_status"], "fetch_status": row.get("fetch_status"),
            "http_status": row.get("http_status"), "verification_reason": row.get("verification_reason"),
            "geo_eligibility_status": row["geo_eligibility_status"], "geo_opportunity_status": row["geo_opportunity_status"],
            "identity_evidence": evidence.get("identity_token_evidence") or saved_verification.get("identity_tokens", ""),
            "inspected_pages": len(pages), "visible_text_characters": sum(int(page.get("visible_text_length") or 0) for page in pages if page.get("fetch_status") in {"SUCCESS", "PARTIAL"}),
            "contact_page_signal": bool(signals.get("contact_page")),
            "contact_status": row["contact_status"], "inspected_at": row.get("last_inspected_at"),
            "source_record_id": row["source_record_id"],
        })
    verification_path = output_dir / "phase6e_website_verification.csv"
    verification_fields = list(verification_rows[0]) if verification_rows else ["candidate_id", "company_name", "website", "canonical_domain", "verification_status", "fetch_status", "http_status", "verification_reason", "geo_eligibility_status", "geo_opportunity_status", "identity_evidence", "inspected_pages", "visible_text_characters", "contact_page_signal", "contact_status", "inspected_at", "source_record_id"]
    with verification_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=verification_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(verification_rows)
    counts[verification_path.name] = len(verification_rows)
    return counts


def audit_profile(db_path: Path = CANDIDATE_DB) -> dict[str, Any]:
    """Structured audit of source tags, geography, domain patterns and multiplicity."""
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        categories = [dict(r) for r in db.execute("SELECT industry,COUNT(*) AS count FROM candidates GROUP BY industry ORDER BY count DESC")]
        geographies = [dict(r) for r in db.execute("SELECT location,COUNT(*) AS count FROM candidates WHERE location<>'' GROUP BY location ORDER BY count DESC LIMIT 100")]
        stats = {
            "total_candidates": total,
            "with_location": db.execute("SELECT COUNT(*) FROM candidates WHERE location<>''").fetchone()[0],
            "with_org_or_gov_suffix": db.execute("SELECT COUNT(*) FROM candidates WHERE canonical_domain LIKE '%.org' OR canonical_domain LIKE '%.gov' OR canonical_domain LIKE '%.edu'").fetchone()[0],
            "punycode_domains": db.execute("SELECT COUNT(*) FROM candidates WHERE canonical_domain LIKE '%xn--%'").fetchone()[0],
            "domains_over_40_chars": db.execute("SELECT COUNT(*) FROM candidates WHERE length(canonical_domain)>40").fetchone()[0],
            "hosted_platform_domains": db.execute("SELECT COUNT(*) FROM candidates WHERE canonical_domain LIKE '%.square.site' OR canonical_domain LIKE '%.lodgify.com' OR canonical_domain LIKE '%.wixsite.com' OR canonical_domain LIKE '%.wix.com' OR canonical_domain LIKE '%.wordpress.com' OR canonical_domain LIKE '%.myshopify.com' OR canonical_domain LIKE '%.weebly.com' OR canonical_domain LIKE '%.squarespace.com' OR canonical_domain LIKE '%.business.site' OR canonical_domain LIKE '%.godaddysites.com'").fetchone()[0],
            "multiple_osm_provenance_objects": db.execute("SELECT COUNT(*) FROM (SELECT candidate_id FROM candidate_provenance GROUP BY candidate_id HAVING COUNT(*)>1)").fetchone()[0],
            "duplicate_company_names_across_domains": db.execute("SELECT COUNT(*) FROM (SELECT lower(trim(company_name)) FROM candidates GROUP BY lower(trim(company_name)) HAVING COUNT(DISTINCT canonical_domain)>1)").fetchone()[0],
            "previously_verified": db.execute("SELECT COUNT(*) FROM candidates WHERE verification_status='VERIFIED'").fetchone()[0],
        }
    return {**stats, "categories": categories, "geography_top_100": geographies}


def phase6e_summary(db_path: Path = PHASE6E_DB) -> dict[str, Any]:
    if not db_path.exists():
        return {"candidate_count": 0, "verified": 0, "geo_eligible": 0, "with_email": 0, "outreach_ready": 0, "screen_statuses": {}}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        counts = dict(db.execute("SELECT screen_status,COUNT(*) FROM phase6e_screening GROUP BY screen_status"))
        osm_count = db.execute("SELECT COUNT(*) FROM candidates WHERE source_batch<>'phase6a-existing-537'").fetchone()[0]
        baseline_count = db.execute("SELECT COUNT(*) FROM candidates WHERE source_batch='phase6a-existing-537'").fetchone()[0]
        row = db.execute("""SELECT COUNT(*),SUM(verification_status='VERIFIED'),SUM(geo_eligibility_status='ELIGIBLE'),
            SUM(contact_status='CONTACT_FOUND'),SUM(outreach_ready_status='READY') FROM candidates""").fetchone()
        return {"candidate_count": row[0] or 0, "verified": row[1] or 0, "geo_eligible": row[2] or 0,
                "with_email": row[3] or 0, "outreach_ready": row[4] or 0, "screen_statuses": counts,
                "osm_candidate_count": osm_count, "existing_cohort_count": baseline_count}


def list_phase6e_candidates(*, q: str = "", source: str = "", status: str = "", screen_status: str = "",
                            page: int = 1, page_size: int = 50, db_path: Path = PHASE6E_DB) -> dict[str, Any]:
    if not db_path.exists():
        return {"candidates": [], "total": 0, "page": page, "page_size": page_size}
    clauses, args = [], []
    if q:
        clauses.append("(c.company_name LIKE ? COLLATE NOCASE OR c.canonical_domain LIKE ? COLLATE NOCASE)")
        args.extend((f"%{q}%", f"%{q}%"))
    if source:
        clauses.append("c.source_name=?")
        args.append(source)
    if status:
        clauses.append("c.verification_status=?")
        args.append(status)
    if screen_status:
        clauses.append("s.screen_status=?")
        args.append(screen_status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    query = "SELECT c.*,s.screen_status,s.priority_score,s.priority_basis,s.category_group,s.chain_location_concern,s.website_domain_concern FROM candidates c JOIN phase6e_screening s USING(candidate_id)" + where
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) FROM candidates c JOIN phase6e_screening s USING(candidate_id)" + where, args).fetchone()[0]
        rows = [dict(row) for row in db.execute(query + " ORDER BY s.priority_score DESC,s.category_group,c.company_name COLLATE NOCASE,c.candidate_id LIMIT ? OFFSET ?", (*args, page_size, (page - 1) * page_size))]
        return {"candidates": rows, "total": total, "page": page, "page_size": page_size}
