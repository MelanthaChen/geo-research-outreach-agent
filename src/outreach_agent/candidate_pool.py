"""Isolated candidate layer for licensed bulk-source discovery (Phase 6D)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import time
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from outreach_agent.normalization import normalize_domain
from outreach_agent.wikidata import is_third_party_profile

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_DB = ROOT / "data/phase6a_experimental.db"
CANDIDATE_DB = ROOT / "data/phase6d_candidates.db"
OSM_SOURCE = "OpenStreetMap (Geofabrik US Northeast extract)"
OSM_SOURCE_URL = "https://download.geofabrik.de/north-america/us-northeast-261014.osm.pbf"
OSM_LICENSE = "OpenStreetMap contributors; ODbL 1.0; attribution required; share-alike on public distribution"
THIRD_PARTY_HOSTS = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "yelp.com", "yellowpages.com", "mapquest.com", "tripadvisor.com", "foursquare.com", "chamberofcommerce.com", "bbb.org", "google.com", "wikipedia.org")


def init_candidate_db(path: Path = CANDIDATE_DB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS candidates (
          candidate_id TEXT PRIMARY KEY, company_name TEXT NOT NULL, website TEXT NOT NULL,
          canonical_domain TEXT NOT NULL UNIQUE, industry TEXT, location TEXT,
          source_name TEXT NOT NULL, source_url TEXT NOT NULL, source_record_id TEXT NOT NULL,
          source_license_or_permission TEXT NOT NULL, discovery_timestamp TEXT NOT NULL,
          verification_status TEXT NOT NULL DEFAULT 'DISCOVERED_UNVERIFIED',
          geo_eligibility_status TEXT NOT NULL DEFAULT 'UNKNOWN',
          geo_opportunity_status TEXT NOT NULL DEFAULT 'UNKNOWN',
          contact_status TEXT NOT NULL DEFAULT 'NOT_DISCOVERED',
          human_review_status TEXT NOT NULL DEFAULT 'PENDING',
          outreach_ready_status TEXT NOT NULL DEFAULT 'NOT_READY',
          verification_reason TEXT, qualification_evidence TEXT,
          fetch_status TEXT, http_status INTEGER, evidence_json TEXT,
          last_inspected_at TEXT, source_batch TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_provenance (
          candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id), source_name TEXT NOT NULL,
          source_url TEXT NOT NULL, source_record_id TEXT NOT NULL,
          source_license_or_permission TEXT NOT NULL, discovery_timestamp TEXT NOT NULL,
          PRIMARY KEY(candidate_id, source_name, source_record_id)
        );
        CREATE TABLE IF NOT EXISTS import_runs (
          source_batch TEXT PRIMARY KEY, source_name TEXT NOT NULL, source_url TEXT NOT NULL,
          source_license_or_permission TEXT NOT NULL, acquired_at TEXT NOT NULL,
          source_sha256 TEXT NOT NULL, parsed_records INTEGER NOT NULL DEFAULT 0,
          missing_website INTEGER NOT NULL DEFAULT 0, rejected_urls INTEGER NOT NULL DEFAULT 0,
          active_duplicates INTEGER NOT NULL DEFAULT 0, duplicate_domains INTEGER NOT NULL DEFAULT 0,
          imported_unique INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(verification_status, geo_eligibility_status);
        CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source_name);
        """)


def _website(value: str) -> str | None:
    value = (value or "").strip()
    if not value or value.lower() in {"yes", "no", "none", "unknown", "n/a"}:
        return None
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        domain = normalize_domain(value)
    except ValueError:
        return None
    if "." not in domain or domain.endswith((".invalid", ".example", ".test", ".localhost")):
        return None
    if is_third_party_profile(value) or any(domain == host or domain.endswith("." + host) for host in THIRD_PARTY_HOSTS):
        return None
    return value


def _has_usable_first_party_channel(company: dict[str, Any], contacts: list[sqlite3.Row]) -> bool:
    suitable = {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "NAMED_PERSON_EMAIL", "SALES_MARKETING_EMAIL", "CONTACT_FORM"}
    domain = company.get("normalized_domain") or ""
    for contact in contacts:
        item = dict(contact)
        if item.get("channel_type") not in suitable or item.get("review_status") == "REJECTED":
            continue
        source = item.get("source_url") or item.get("channel_url") or ""
        try:
            if normalize_domain(source) != domain:
                continue
            if item["channel_type"] == "CONTACT_FORM":
                if item.get("channel_url") and normalize_domain(item["channel_url"]) == domain:
                    return True
            elif item.get("email") and normalize_domain(item["email"].rsplit("@", 1)[-1]) == domain:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def parse_osm_pbf(pbf: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Extract named commercial POIs with a first-party website from an OSM PBF."""
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("Install with `uv sync --extra openstreetmap` to parse PBF files") from exc

    rows: list[dict[str, str]] = []
    stats = Counter()
    commercial_amenities = {"restaurant", "cafe", "fast_food", "pub", "bar", "bank", "pharmacy", "clinic", "doctors", "dentist", "veterinary", "beauty_salon", "hairdresser", "car_repair", "car_wash", "fuel", "hotel", "fitness_centre", "theatre", "cinema", "coworking_space", "marketplace", "childcare", "language_school", "college", "university", "hospital"}
    office_types = {"company", "insurance", "estate_agent", "accountant", "consultancy", "travel_agent", "employment_agency", "lawyer", "financial", "it", "advertising_agency", "marketing", "tax_advisor", "educational_institution"}

    class Handler(osmium.SimpleHandler):
        def _append(self, obj, element_type: str) -> None:
            tags = obj.tags
            name = tags.get("name") or tags.get("brand")
            if not name:
                return
            stats["named_objects"] += 1
            website = _website(tags.get("website") or tags.get("contact:website") or "")
            if not website:
                stats["missing_or_rejected_website"] += 1
                return
            if tags.get("disused") == "yes" or tags.get("abandoned") == "yes" or tags.get("demolished") == "yes" or tags.get("vacant") == "yes":
                stats["inactive_tag_excluded"] += 1
                return
            commercial = bool(tags.get("shop") and tags.get("shop") not in {"vacant", "no"}) or tags.get("amenity") in commercial_amenities or tags.get("office") in office_types or bool(tags.get("craft") and tags.get("craft") not in {"no", "abandoned"}) or tags.get("tourism") in {"hotel", "guest_house", "museum", "attraction"}
            if not commercial:
                return
            website = website.replace("&amp;", "&")
            canonical = normalize_domain(website)
            location = ", ".join(part for part in (tags.get("addr:city"), tags.get("addr:state"), tags.get("addr:country")) if part)
            industry = next((f"{key}={tags.get(key)}" for key in ("shop", "office", "amenity", "craft", "tourism") if tags.get(key)), "")
            rows.append({"company_name": name.strip(), "website": website, "canonical_domain": canonical,
                         "industry": industry, "location": location, "element_type": element_type,
                         "source_record_id": f"{element_type}/{obj.id}", "source_url": f"https://www.openstreetmap.org/{element_type}/{obj.id}"})
            stats["website_bearing_commercial_objects"] += 1

        def node(self, n) -> None:
            self._append(n, "node")

        def way(self, w) -> None:
            self._append(w, "way")

        def relation(self, r) -> None:
            self._append(r, "relation")

    Handler().apply_file(str(pbf), locations=False)
    stats["raw_objects"] = len(rows)
    return rows, dict(stats)


def import_osm_records(rows: list[dict[str, str]], active_db: Path = ACTIVE_DB,
                       candidate_db: Path = CANDIDATE_DB, source_hash: str = "") -> dict[str, int]:
    init_candidate_db(candidate_db)
    with sqlite3.connect(f"file:{active_db.resolve()}?mode=ro", uri=True) as active:
        active_domains = {str(row[0]).lower().removeprefix("www.") for row in active.execute("SELECT normalized_domain FROM companies")}
    now = datetime.now(timezone.utc).isoformat()
    batch = "geofabrik-us-northeast-261014"
    imported: dict[str, dict[str, str]] = {}
    active_dupes = domain_dupes = 0
    for row in rows:
        domain = row["canonical_domain"]
        if domain in active_domains:
            active_dupes += 1
            continue
        # The first source element for a domain becomes the stable primary record;
        # all distinct elements are then appended to provenance below.
        imported.setdefault(domain, row)
    domain_dupes = max(0, len(rows) - active_dupes - len(imported))
    with sqlite3.connect(candidate_db) as db:
        existing_candidate_domains = {str(row[0]) for row in db.execute("SELECT canonical_domain FROM candidates")}
        newly_imported = len(set(imported) - existing_candidate_domains)
        db.execute("INSERT OR IGNORE INTO import_runs(source_batch,source_name,source_url,source_license_or_permission,acquired_at,source_sha256,parsed_records,active_duplicates,duplicate_domains,imported_unique) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (batch, OSM_SOURCE, OSM_SOURCE_URL, OSM_LICENSE, now, source_hash, len(rows), active_dupes, domain_dupes, newly_imported))
        for domain, row in imported.items():
            candidate_id = "osm:" + hashlib.sha256(domain.encode()).hexdigest()[:24]
            db.execute("""INSERT OR IGNORE INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,source_batch)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (candidate_id,row["company_name"],row["website"],domain,row.get("industry"),row.get("location"),OSM_SOURCE,row["source_url"],row["source_record_id"],OSM_LICENSE,now,batch))
        for row in rows:
            domain = row["canonical_domain"]
            if domain in active_domains or domain not in imported:
                continue
            candidate_id = "osm:" + hashlib.sha256(domain.encode()).hexdigest()[:24]
            db.execute("INSERT OR IGNORE INTO candidate_provenance VALUES(?,?,?,?,?,?)",
                       (candidate_id, OSM_SOURCE, row["source_url"], row["source_record_id"], OSM_LICENSE, now))
        db.commit()
        actual = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        provenance = db.execute("SELECT COUNT(*) FROM candidate_provenance").fetchone()[0]
    return {"parsed_records": len(rows), "active_duplicates": active_dupes, "duplicate_domains": domain_dupes,
            "source_unique_after_active_dedup": len(imported), "imported_unique": newly_imported,
            "candidate_rows": actual, "provenance_rows": provenance}


def candidate_summary(path: Path = CANDIDATE_DB) -> dict[str, Any]:
    if not path.exists():
        return {"candidate_count": 0, "verified": 0, "geo_eligible": 0, "with_email": 0, "outreach_ready": 0}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        row = db.execute("""SELECT COUNT(*), SUM(verification_status='VERIFIED'), SUM(geo_eligibility_status='ELIGIBLE'),
           SUM(contact_status='CONTACT_FOUND'), SUM(outreach_ready_status='READY') FROM candidates""").fetchone()
        return {"candidate_count": row[0] or 0, "verified": row[1] or 0, "geo_eligible": row[2] or 0,
                "with_email": row[3] or 0, "outreach_ready": row[4] or 0}


def list_candidates(path: Path = CANDIDATE_DB, *, q: str = "", source: str = "", status: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
    if not path.exists():
        return {"candidates": [], "total": 0, "page": page, "page_size": page_size}
    clauses: list[str] = []
    args: list[Any] = []
    if q:
        clauses.append("(company_name LIKE ? COLLATE NOCASE OR canonical_domain LIKE ? COLLATE NOCASE)")
        args.extend((f"%{q}%", f"%{q}%"))
    if source:
        clauses.append("source_name = ?")
        args.append(source)
    if status:
        clauses.append("verification_status = ?")
        args.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) FROM candidates" + where, args).fetchone()[0]
        rows = [dict(row) for row in db.execute("SELECT * FROM candidates" + where + " ORDER BY company_name COLLATE NOCASE, candidate_id LIMIT ? OFFSET ?", (*args, page_size, (page - 1) * page_size))]
    return {"candidates": rows, "total": total, "page": page, "page_size": page_size}


EXPORT_FIELDS = ("candidate_id", "company_name", "website", "canonical_domain", "industry", "location", "source_name", "source_url", "source_record_id", "source_license_or_permission", "discovery_timestamp", "verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "human_review_status", "outreach_ready_status")


def export_candidate_csv(kind: str, path: Path = CANDIDATE_DB, *, include_active: bool | None = None) -> str:
    clauses = {"all_candidates": "1=1", "verified_companies": "verification_status='VERIFIED'", "geo_eligible_companies": "geo_eligibility_status='ELIGIBLE'", "contact_candidates": "contact_status='CONTACT_FOUND'"}
    if kind not in clauses:
        raise ValueError(f"Unknown candidate export: {kind}")
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=EXPORT_FIELDS, lineterminator="\n")
    writer.writeheader()
    if path.exists():
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute(f"SELECT {','.join(EXPORT_FIELDS)} FROM candidates WHERE {clauses[kind]} ORDER BY canonical_domain"):
                values = dict(row)
                for key, value in values.items():
                    if isinstance(value, str) and value[:1] in "=+-@":
                        values[key] = "'" + value
                writer.writerow(values)
    # The public candidate exports describe the full pool: the preserved active
    # 537 plus the separate newly acquired layer. Never write back to the active DB.
    if include_active is None:
        include_active = path.resolve() == CANDIDATE_DB.resolve()
    if include_active and kind in {"all_candidates", "verified_companies", "geo_eligible_companies", "contact_candidates"} and ACTIVE_DB.exists():
        from outreach_agent.dashboard_data import _verify_saved_snapshot
        verification: dict[int, str] = {}
        for source_file in (ROOT / "data/phase4e_website_verification.csv", ROOT / "data/phase4f_website_verification.csv"):
            if source_file.exists():
                with source_file.open(encoding="utf-8-sig", newline="") as handle:
                    for item in csv.DictReader(handle):
                        if item.get("company_id") and item.get("verification_status"):
                            verification[int(item["company_id"])] = item["verification_status"].upper()
        with sqlite3.connect(f"file:{ACTIVE_DB.resolve()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for company in db.execute("SELECT * FROM companies ORDER BY id"):
                company = dict(company)
                status = verification.get(int(company["id"])) or _verify_saved_snapshot(db, company)
                eligibility = (company.get("eligibility") or "NEEDS_REVIEW").upper()
                saved_contacts = db.execute("SELECT channel_type,email,channel_url,source_url,review_status FROM contacts WHERE company_id=?", (company["id"],)).fetchall()
                has_contact = _has_usable_first_party_channel(company, saved_contacts)
                ready = (company.get("pipeline_status") == "READY_FOR_GEO" and status == "VERIFIED" and eligibility == "ELIGIBLE")
                if kind == "verified_companies" and status != "VERIFIED":
                    continue
                if kind == "geo_eligible_companies" and eligibility != "ELIGIBLE":
                    continue
                if kind == "contact_candidates" and not has_contact:
                    continue
                provenance = db.execute("SELECT source_name,source_url,source_identifier FROM discovery_records WHERE company_id=? ORDER BY id", (company["id"],)).fetchall()
                names = "; ".join(sorted({item[0] or "Unknown" for item in provenance})) or "Legacy cohort"
                source_url = next((item[1] for item in provenance if item[1]), "")
                record_id = ";".join(item[2] or "" for item in provenance if item[2])
                values = {
                    "candidate_id": f"phase6a:{company['id']}", "company_name": company.get("company_name"),
                    "website": company.get("website"), "canonical_domain": company.get("normalized_domain"),
                    "industry": company.get("industry"), "location": company.get("location"), "source_name": names,
                    "source_url": source_url, "source_record_id": record_id,
                    "source_license_or_permission": "Legacy provenance; source-specific reuse status is recorded in phase6a_source_inventory.csv and was not re-evaluated in Phase 6D",
                    "discovery_timestamp": "", "verification_status": status,
                    "geo_eligibility_status": eligibility, "geo_opportunity_status": (company.get("geo_opportunity") or "UNKNOWN").upper(),
                    "contact_status": "CONTACT_FOUND" if has_contact else (company.get("contact_status") or "NOT_DISCOVERED"),
                    "human_review_status": company.get("review_status") or "PENDING",
                    "outreach_ready_status": "READY" if ready else "NOT_READY",
                }
                for key, value in values.items():
                    if isinstance(value, str) and value[:1] in "=+-@":
                        values[key] = "'" + value
                writer.writerow(values)
    return out.getvalue()


def verify_candidate_batch(limit: int = 25, *, candidate_db: Path = CANDIDATE_DB,
                          rules_path: Path = ROOT / "config/qualification.yaml",
                          candidate_ids: list[str] | None = None,
                          request_budget: int | None = None,
                          time_budget_seconds: float | None = None,
                          include_contact_evidence: bool = False,
                          request_timeout_seconds: float = 12,
                          retry_count: int = 1,
                          delay_between_requests_seconds: float = 1,
                          max_concurrency: int = 1,
                          check_sitemap: bool = True) -> dict[str, Any]:
    """Inspect a resumable, bounded batch using the project's identity/qualification rules.

    Page-level business contact evidence is retained only when explicitly enabled.
    No contact approval is written. A later call resumes at the next pending row.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Batch limit must be between 1 and 100")
    import yaml
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from outreach_agent.config import WebsiteSettings
    from outreach_agent.qualification import evaluate
    from outreach_agent.website import RequestBudgetExceeded, WebsiteCollector
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    if request_timeout_seconds <= 0 or retry_count < 0 or delay_between_requests_seconds < 0 or not 1 <= max_concurrency <= 8:
        raise ValueError("timeout must be positive; retry/delay cannot be negative; concurrency must be 1..8")
    settings = WebsiteSettings(max_companies_per_run=min(limit, 25), max_pages_per_company=2,
                               request_timeout_seconds=request_timeout_seconds,
                               delay_between_requests_seconds=delay_between_requests_seconds,
                               retry_count=retry_count, max_response_bytes=1_000_000)
    if candidate_ids is not None and len(candidate_ids) > limit:
        raise ValueError("candidate_ids cannot exceed the batch limit")
    if request_budget is not None and request_budget < 1:
        raise ValueError("request_budget must be at least 1")
    if time_budget_seconds is not None and time_budget_seconds <= 0:
        raise ValueError("time_budget_seconds must be positive")
    with sqlite3.connect(candidate_db) as db:
        db.row_factory = sqlite3.Row
        if candidate_ids is None:
            candidates = [dict(row) for row in db.execute("SELECT * FROM candidates WHERE verification_status='DISCOVERED_UNVERIFIED' ORDER BY candidate_id LIMIT ?", (limit,))]
        else:
            candidates = []
            for candidate_id in candidate_ids:
                row = db.execute("SELECT * FROM candidates WHERE candidate_id=? AND verification_status='DISCOVERED_UNVERIFIED'", (candidate_id,)).fetchone()
                if row is not None:
                    candidates.append(dict(row))
    counts: Counter[str] = Counter()
    started = time.monotonic()
    if request_budget is None and check_sitemap:
        collector = WebsiteCollector(settings)
    else:
        collector = WebsiteCollector(settings, request_budget=request_budget, check_sitemap=check_sitemap)
    processed = 0
    time_budget_exhausted = False
    stage_totals: Counter[str] = Counter()

    def process_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        candidate_metrics: Counter[str] = Counter()
        if time_budget_seconds is not None and time.monotonic() - started >= time_budget_seconds:
            return {"attempted": False, "time_budget_exhausted": True}
        if getattr(collector, "request_budget_exhausted", False):
            return {"attempted": False, "request_budget_exhausted": True}
        company = types.SimpleNamespace(id=candidate["candidate_id"], company_name=candidate["company_name"],
            website=candidate["website"], normalized_domain=candidate["canonical_domain"])
        try:
            phase_started = time.perf_counter()
            snapshot = collector.inspect(company)
            candidate_metrics["website_inspection_seconds"] += time.perf_counter() - phase_started
            if getattr(collector, "request_budget_exhausted", False) and not any(page.fetch_status in {"SUCCESS", "PARTIAL"} for page in snapshot.pages):
                return {"attempted": False, "request_budget_exhausted": True}
            phase_started = time.perf_counter()
            identity = verify_identity_for_candidate(company, snapshot)
            candidate_metrics["identity_decision_seconds"] += time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            if identity.status == "VERIFIED":
                signals = {"discovery_source": candidate["source_name"], "website_evidence": json.loads(snapshot.extracted_signals or "{}"), "fetch_status": snapshot.fetch_status}
                qualification = evaluate(signals, rules)
                eligibility, opportunity, qualification_evidence = qualification.eligibility, qualification.geo_opportunity, qualification.evidence
            else:
                eligibility, opportunity = "NEEDS_REVIEW", "UNKNOWN"
                qualification_evidence = {"status": "NOT_APPLIED_IDENTITY_UNVERIFIED", "reason": identity.reason}
            candidate_metrics["geo_qualification_seconds"] += time.perf_counter() - phase_started
            safe_pages = []
            for page in snapshot.pages:
                try:
                    raw_channels = json.loads(getattr(page, "extracted_channels", "[]") or "[]")
                except (json.JSONDecodeError, TypeError):
                    raw_channels = []
                allowed_channels = []
                if include_contact_evidence:
                    for channel in raw_channels if isinstance(raw_channels, list) else []:
                        channel_type = str(channel.get("type", "")).split(".")[-1]
                        if channel_type in {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "CONTACT_FORM"}:
                            allowed_channels.append({key: value for key, value in channel.items() if key not in {"name", "title"}})
                safe_pages.append({"requested_url": page.requested_url, "final_url": page.final_url, "http_status": page.http_status,
                    "title": page.title, "meta_description": page.meta_description, "content_hash": page.content_hash,
                    "visible_text_length": page.visible_text_length, "structured_data_types": page.structured_data_types,
                    "fetch_status": page.fetch_status, "extracted_channels": json.dumps(allowed_channels, sort_keys=True)})
            evidence = {"pages": safe_pages, "signals": json.loads(snapshot.extracted_signals or "{}"),
                "identity_token_evidence": identity.identity_tokens, "qualification": qualification_evidence,
                "source_record_id": candidate["source_record_id"]}
            values = (identity.status, eligibility, opportunity, identity.reason,
                json.dumps(qualification_evidence, sort_keys=True), snapshot.fetch_status, snapshot.http_status,
                json.dumps(evidence, sort_keys=True), datetime.now(timezone.utc).isoformat(), candidate["candidate_id"])
            phase_started = time.perf_counter()
            with sqlite3.connect(candidate_db) as db:
                db.execute("""UPDATE candidates SET verification_status=?,geo_eligibility_status=?,geo_opportunity_status=?,verification_reason=?,
                             qualification_evidence=?,fetch_status=?,http_status=?,evidence_json=?,last_inspected_at=? WHERE candidate_id=?""", values)
                db.commit()
            candidate_metrics["checkpoint_persistence_seconds"] += time.perf_counter() - phase_started
            return {"attempted": True, "identity": identity.status, "eligibility": eligibility,
                    "stage_seconds": candidate_metrics}
        except RequestBudgetExceeded:
            return {"attempted": False, "request_budget_exhausted": True, "stage_seconds": candidate_metrics}
        except Exception as exc:  # one bad site must not stop the bounded batch
            with sqlite3.connect(candidate_db) as db:
                db.execute("UPDATE candidates SET verification_status='FETCH_FAILED',geo_eligibility_status='UNKNOWN',fetch_status='FETCH_FAILED',verification_reason=?,last_inspected_at=? WHERE candidate_id=?",
                    (f"Inspection error: {type(exc).__name__}: {exc}"[:1000], datetime.now(timezone.utc).isoformat(), candidate["candidate_id"]))
                db.commit()
            return {"attempted": True, "processing_error": True, "stage_seconds": candidate_metrics}

    try:
        with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="phase7-crawl") as executor:
            futures = [executor.submit(process_candidate, candidate) for candidate in candidates]
            for future in as_completed(futures):
                result = future.result()
                if result.get("attempted"):
                    processed += 1
                    if result.get("processing_error"):
                        counts["processing_error"] += 1
                    else:
                        counts[result["identity"]] += 1
                        counts[f"eligibility:{result['eligibility']}"] += 1
                time_budget_exhausted |= bool(result.get("time_budget_exhausted"))
                for stage, seconds in result.get("stage_seconds", {}).items():
                    stage_totals[stage] += seconds
    finally:
        collector.close()
    with sqlite3.connect(candidate_db) as db:
        remaining = db.execute("SELECT COUNT(*) FROM candidates WHERE verification_status='DISCOVERED_UNVERIFIED'").fetchone()[0]
    return {"attempted": processed, "remaining": remaining,
            "runtime_seconds": round(time.monotonic() - started, 2), "requests_attempted": collector.website_requests_attempted,
            "requests_succeeded": getattr(collector, "website_requests_succeeded", 0), "requests_failed": getattr(collector, "website_requests_failed", 0),
            "retries": collector.retry_attempts, "request_budget": request_budget,
            "request_budget_exhausted": getattr(collector, "request_budget_exhausted", False),
            "time_budget_exhausted": time_budget_exhausted, "counts": dict(counts),
            "stage_seconds": dict(stage_totals), "request_metrics": collector.request_metrics() if hasattr(collector, "request_metrics") else {},
            "check_sitemap": check_sitemap, "max_concurrency": max_concurrency}


def verify_identity_for_candidate(company: Any, snapshot: Any):
    from outreach_agent.website_identity import verify_company_website
    return verify_company_website(company, snapshot)
