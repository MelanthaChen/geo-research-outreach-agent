"""Property-level, ODbL-attributed hotel discovery and bounded validation."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.candidate_pool import ROOT, THIRD_PARTY_HOSTS, verify_candidate_batch
from outreach_agent.normalization import normalize_domain

HOTEL_DB = ROOT / "data/hotel_candidates.db"
HOTEL_PBF = ROOT / "data/raw_sources/us-northeast-261014.osm.pbf"
HOTEL_EXPORT = ROOT / "data/hotel_candidates.csv"
HOTEL_VERIFIED_EXPORT = ROOT / "data/hotel_verified_properties.csv"
HOTEL_ELIGIBLE_EXPORT = ROOT / "data/hotel_geo_eligible_properties.csv"
HOTEL_CONTACT_EXPORT = ROOT / "data/hotel_contact_candidates.csv"
HOTEL_FAILURE_EXPORT = ROOT / "data/hotel_validation_failures.csv"
GEO_PROFILE_EXPORT = ROOT / "data/geo_engine_hotel_profiles.jsonl"
STARTUP_PROFILE_EXPORT = ROOT / "data/geo_engine_startup_profiles.jsonl"
STARTUP_PROFILE_DB = ROOT / "data/phase7_candidates.db"
HOTEL_LICENSE = "OpenStreetMap contributors; ODbL 1.0; attribution required; share-alike on public distribution"
HOTEL_SOURCE = "OpenStreetMap via Geofabrik US Northeast extract"
HOTEL_NONFIRST_PARTY_HOSTS = set(THIRD_PARTY_HOSTS) | {
    "booking.com", "hotels.com", "expedia.com", "agoda.com", "trivago.com", "kayak.com",
    "hotwire.com", "priceline.com", "orbitz.com", "hostelworld.com", "airbnb.com",
    "vrbo.com", "trip.com", "travelocity.com", "hoteltonight.com",
}


def init_hotel_db(path: Path = HOTEL_DB) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS candidates (
          candidate_id TEXT PRIMARY KEY, company_name TEXT NOT NULL, website TEXT NOT NULL DEFAULT '',
          canonical_domain TEXT NOT NULL DEFAULT '', industry TEXT, location TEXT,
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
          last_inspected_at TEXT, source_batch TEXT NOT NULL,
          property_type TEXT NOT NULL DEFAULT 'hotel', address TEXT NOT NULL DEFAULT '',
          chain_name TEXT NOT NULL DEFAULT '', chain_status TEXT NOT NULL DEFAULT 'UNKNOWN',
          amenities_json TEXT NOT NULL DEFAULT '[]', room_evidence_json TEXT NOT NULL DEFAULT '[]',
          guest_segments_json TEXT NOT NULL DEFAULT '[]',
          operating_evidence_status TEXT NOT NULL DEFAULT 'NOT_ASSESSED'
        );
        CREATE INDEX IF NOT EXISTS hotel_status_idx ON candidates(verification_status,geo_eligibility_status);
        CREATE INDEX IF NOT EXISTS hotel_domain_idx ON candidates(canonical_domain);
        CREATE TABLE IF NOT EXISTS import_runs (
          source_batch TEXT PRIMARY KEY, source_file TEXT NOT NULL, source_sha256 TEXT NOT NULL,
          imported_at TEXT NOT NULL, parsed_hotel_objects INTEGER NOT NULL,
          named_properties INTEGER NOT NULL, website_candidates INTEGER NOT NULL,
          duplicate_source_ids INTEGER NOT NULL DEFAULT 0, source_url TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS hotel_runs (
          run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL, attempted INTEGER NOT NULL, requests INTEGER NOT NULL,
          counts_json TEXT NOT NULL, report_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hotel_contact_channels (
          candidate_id TEXT NOT NULL, channel_type TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
          channel_url TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL, evidence TEXT NOT NULL,
          purpose TEXT NOT NULL DEFAULT '', review_status TEXT NOT NULL DEFAULT 'PENDING',
          discovered_at TEXT NOT NULL, PRIMARY KEY(candidate_id,channel_type,email,channel_url)
        );
        CREATE TABLE IF NOT EXISTS hotel_provenance (
          candidate_id TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT NOT NULL,
          source_record_id TEXT NOT NULL, source_license_or_permission TEXT NOT NULL,
          source_dataset_url TEXT NOT NULL, source_file TEXT NOT NULL, discovery_timestamp TEXT NOT NULL,
          PRIMARY KEY(candidate_id,source_file,source_record_id)
        );
        """)
        import_columns = {row[1] for row in db.execute("PRAGMA table_info(import_runs)")}
        if "source_url" not in import_columns:
            db.execute("ALTER TABLE import_runs ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in db.execute("PRAGMA table_info(candidates)")}
        if "operating_evidence_status" not in columns:
            db.execute("ALTER TABLE candidates ADD COLUMN operating_evidence_status TEXT NOT NULL DEFAULT 'NOT_ASSESSED'")


def _hotel_rows(pbf: Path) -> tuple[list[dict[str, str]], dict[str, int]]:
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("Install osmium via `uv sync --extra openstreetmap` to read the local OSM extract") from exc
    rows: list[dict[str, str]] = []
    stats: Counter[str] = Counter()
    now = datetime.now(timezone.utc).isoformat()
    region = pbf.name.removesuffix(".osm.pbf")
    label_region = region.removesuffix("-latest")
    if label_region.startswith("us-northeast-"):
        label_region = "us-northeast"
    label = label_region.replace("-", " ").title()
    source_name = f"OpenStreetMap via Geofabrik {label} extract"
    facility_keys = ("internet_access", "wheelchair", "parking", "swimming_pool", "sauna", "spa", "fitness_centre", "restaurant", "bar", "breakfast", "air_conditioning", "pets", "rooms", "beds", "stars", "hotel", "tourism")
    address_keys = ("addr:housenumber", "addr:street", "addr:unit", "addr:city", "addr:state", "addr:postcode", "addr:country")

    class Handler(osmium.SimpleHandler):
        def _append(self, obj: Any, element_type: str) -> None:
            tags = obj.tags
            if tags.get("tourism") != "hotel" and tags.get("amenity") != "hotel":
                return
            stats["hotel_objects"] += 1
            name = (tags.get("name") or tags.get("brand") or "").strip()
            if not name:
                stats["unnamed_hotel_objects"] += 1
                return
            if any(tags.get(key) in {"yes", "true"} for key in ("disused", "abandoned", "demolished", "vacant")):
                stats["inactive_tagged_objects"] += 1
                return
            raw_website = tags.get("website") or tags.get("contact:website") or ""
            website = raw_website.strip()
            if website and "://" not in website:
                website = "https://" + website
            try:
                domain = normalize_domain(website) if website else ""
            except ValueError:
                domain, website = "", ""
            record_id = f"{element_type}/{obj.id}"
            addresses = [tags.get(key, "").strip() for key in address_keys]
            address = ", ".join(part for part in addresses if part)
            city = tags.get("addr:city") or ""
            state = tags.get("addr:state") or ""
            country = tags.get("addr:country") or ""
            location = ", ".join(part for part in (city, state, country) if part)
            chain = (tags.get("brand") or tags.get("operator") or "").strip()
            amenities = {key: tags[key] for key in facility_keys if tags.get(key)}
            extra_hotel_tags = {tag.k: tag.v for tag in tags if tag.k.startswith(("hotel:", "rooms:", "guest:"))}
            amenities.update(extra_hotel_tags)
            room_keys = ("rooms", "beds", "hotel:rooms")
            rooms = [{"tag": key, "value": tags[key], "source": "OSM tag; not independently verified"} for key in room_keys if tags.get(key)]
            rows.append({
                "candidate_id": f"osm-hotel:{record_id}", "company_name": name, "website": website,
                "canonical_domain": domain, "industry": tags.get("tourism") or tags.get("amenity") or "hotel",
                "location": location, "source_name": source_name,
                "source_url": f"https://www.openstreetmap.org/{record_id}", "source_record_id": record_id,
                "source_license_or_permission": HOTEL_LICENSE, "discovery_timestamp": now,
                "verification_status": "DISCOVERED_UNVERIFIED" if website else "MISSING_OFFICIAL_WEBSITE",
                "geo_eligibility_status": "UNKNOWN", "geo_opportunity_status": "UNKNOWN",
                "contact_status": "NOT_DISCOVERED", "human_review_status": "PENDING",
                "outreach_ready_status": "NOT_READY", "verification_reason": "OSM hotel tag is a discovery signal only; first-party identity and operating evidence required",
                "source_batch": pbf.name, "property_type": "hotel", "address": address,
                "chain_name": chain, "chain_status": "CHAIN_INDICATED" if chain else "INDEPENDENT_OR_UNKNOWN",
                "amenities_json": json.dumps(amenities, sort_keys=True),
                "room_evidence_json": json.dumps(rooms, sort_keys=True), "guest_segments_json": "[]",
                "operating_evidence_status": "NOT_ASSESSED",
            })
            stats["named_hotel_properties"] += 1
            if website:
                stats["website_candidates"] += 1
            else:
                stats["without_official_website"] += 1
            if address:
                stats["with_address"] += 1
            if chain:
                stats["chain_indicated"] += 1
            if rooms:
                stats["with_room_evidence"] += 1

        def node(self, obj: Any) -> None: self._append(obj, "node")
        def way(self, obj: Any) -> None: self._append(obj, "way")
        def relation(self, obj: Any) -> None: self._append(obj, "relation")

    Handler().apply_file(str(pbf), locations=False)
    return rows, dict(stats)


def import_hotels(pbf: Path = HOTEL_PBF, database: Path = HOTEL_DB) -> dict[str, Any]:
    if not pbf.exists():
        raise FileNotFoundError(pbf)
    hasher = hashlib.sha256()
    with pbf.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    rows, stats = _hotel_rows(pbf)
    init_hotel_db(database)
    region = pbf.name.removesuffix(".osm.pbf")
    label_region = region.removesuffix("-latest")
    if label_region.startswith("us-northeast-"):
        label_region = "us-northeast"
    source_dataset_url = ("https://download.geofabrik.de/north-america/us-northeast.html"
                          if label_region == "us-northeast"
                          else f"https://download.geofabrik.de/north-america/us/{label_region}.html")
    source_name = f"OpenStreetMap via Geofabrik {label_region.replace('-', ' ').title()} extract"
    source_batch = f"geofabrik-hotel:{region}"
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with sqlite3.connect(database) as db:
        prior = db.execute("SELECT source_sha256 FROM import_runs WHERE source_batch=?", (source_batch,)).fetchone()
        if prior and prior[0] != digest:
            raise ValueError("The source batch identifier already exists with a different PBF hash")
        for row in rows:
            columns = ",".join(row)
            marks = ",".join("?" for _ in row)
            inserted += db.execute(f"INSERT OR IGNORE INTO candidates({columns}) VALUES({marks})", tuple(row.values())).rowcount
            db.execute("INSERT OR IGNORE INTO hotel_provenance VALUES(?,?,?,?,?,?,?,?)", (
                row["candidate_id"], source_name, row["source_url"], row["source_record_id"],
                HOTEL_LICENSE, source_dataset_url, str(pbf), now))
        db.execute("INSERT OR REPLACE INTO import_runs(source_batch,source_file,source_sha256,imported_at,parsed_hotel_objects,named_properties,website_candidates,duplicate_source_ids,source_url) VALUES(?,?,?,?,?,?,?,?,?)", (
            source_batch, str(pbf), digest, now, stats.get("hotel_objects", 0),
            stats.get("named_hotel_properties", 0), stats.get("website_candidates", 0),
            len(rows) - inserted, source_dataset_url))
        db.commit()
    third_party = _mark_nonfirst_party_websites(database)
    return {"source": source_name, "source_url": source_dataset_url,
            "license": HOTEL_LICENSE, "database": str(database), "inserted_properties": inserted,
            "existing_or_duplicate_source_ids": len(rows) - inserted, "third_party_urls_excluded": third_party,
            "stats": stats}


def _mark_nonfirst_party_websites(database: Path) -> int:
    changed = 0
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT candidate_id,canonical_domain FROM candidates WHERE website<>'' AND verification_status='DISCOVERED_UNVERIFIED'").fetchall()
        for candidate_id, domain in rows:
            normalized = str(domain or "").casefold().removeprefix("www.")
            if any(normalized == host or normalized.endswith("." + host) for host in HOTEL_NONFIRST_PARTY_HOSTS):
                changed += db.execute(
                    "UPDATE candidates SET verification_status='THIRD_PARTY_URL',verification_reason=? WHERE candidate_id=?",
                    ("Source website tag points to a directory, booking aggregator, social profile, or third-party listing; not treated as an official hotel website", candidate_id),
                ).rowcount
        db.commit()
    return changed


def hotel_summary(database: Path = HOTEL_DB) -> dict[str, Any]:
    init_hotel_db(database)
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        statuses = dict(db.execute("SELECT verification_status,COUNT(*) FROM candidates GROUP BY verification_status"))
        geo = dict(db.execute("SELECT geo_eligibility_status,COUNT(*) FROM candidates GROUP BY geo_eligibility_status"))
        n = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        processed = db.execute("SELECT COUNT(*) FROM candidates WHERE last_inspected_at IS NOT NULL").fetchone()[0]
        domains = db.execute("SELECT COUNT(DISTINCT canonical_domain) FROM candidates WHERE canonical_domain<>''").fetchone()[0]
        chain = db.execute("SELECT COUNT(*) FROM candidates WHERE chain_status='CHAIN_INDICATED'").fetchone()[0]
        verified = db.execute("SELECT COUNT(*) FROM candidates WHERE verification_status='VERIFIED'").fetchone()[0]
        eligible = db.execute("SELECT COUNT(*) FROM candidates WHERE geo_eligibility_status='ELIGIBLE'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM candidates WHERE verification_status='DISCOVERED_UNVERIFIED'").fetchone()[0]
        email = {row[0] for row in db.execute("SELECT DISTINCT candidate_id FROM hotel_contact_channels WHERE email<>'' AND review_status<>'REJECTED'")}
        forms = {row[0] for row in db.execute("SELECT DISTINCT candidate_id FROM hotel_contact_channels WHERE channel_type='CONTACT_FORM' AND channel_url<>'' AND review_status<>'REJECTED'")}
        verified_operating_signals = db.execute("SELECT COUNT(*) FROM candidates WHERE operating_evidence_status='OPERATING_SIGNAL_REVIEW_REQUIRED'").fetchone()[0]
    return {"property_candidates": n, "unique_domains": domains, "chain_indicated_properties": chain,
            "processed_properties": processed,
            "verified_properties": verified, "geo_eligible_properties": eligible,
            "properties_with_business_email": len(email), "properties_with_confirmed_form": len(forms),
            "properties_with_both_channels": len(email & forms), "properties_with_any_contact": len(email | forms),
            "properties_with_operating_signals": verified_operating_signals,
            "pending_verification": pending,
            "verification_statuses": statuses, "eligibility_statuses": geo}


def hotel_batch_quality_pause(batch: dict[str, Any], *, minimum_candidates: int = 25,
                              max_access_block_rate: float = 0.40) -> bool:
    """Pause after a complete batch when access blocks are materially above the pilot baseline."""
    attempted = int(batch.get("attempted", 0))
    counts = batch.get("counts", {})
    blocked = int(counts.get("ACCESS_BLOCKED", 0)) if isinstance(counts, dict) else 0
    return attempted >= minimum_candidates and blocked / attempted >= max_access_block_rate


def _persist_hotel_evidence(database: Path) -> dict[str, int]:
    email_types = {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL"}
    channel_counts: Counter[str] = Counter()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT candidate_id,canonical_domain,verification_status,geo_eligibility_status,evidence_json FROM candidates WHERE verification_status='VERIFIED' AND geo_eligibility_status='ELIGIBLE'").fetchall()
        for row in rows:
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except json.JSONDecodeError:
                evidence = {}
            signals = evidence.get("signals", {}) if isinstance(evidence, dict) else {}
            pages = evidence.get("pages", []) if isinstance(evidence, dict) else []
            service_pages = []
            for page in pages if isinstance(pages, list) else []:
                text = " ".join(str(page.get(key) or "") for key in ("title", "meta_description")).casefold()
                types = str(page.get("structured_data_types") or "").casefold()
                if any(token in text for token in ("hotel", "lodge", "inn", "resort", "rooms", "book", "reservation")) or any(token in types for token in ("hotel", "lodgingbusiness", "offer")):
                    service_pages.append({"url": page.get("final_url") or page.get("requested_url"),
                                          "title": page.get("title"), "meta_description": page.get("meta_description"),
                                          "structured_data_types": page.get("structured_data_types")})
                raw_channels = page.get("extracted_channels", "[]")
                try:
                    channels = json.loads(raw_channels) if isinstance(raw_channels, str) else raw_channels
                except json.JSONDecodeError:
                    channels = []
                for channel in channels if isinstance(channels, list) else []:
                    kind = str(channel.get("type") or "").split(".")[-1].upper()
                    email = str(channel.get("email") or "").strip().casefold()
                    route = str(channel.get("channel_url") or channel.get("url") or "").strip()
                    source_url = str(page.get("final_url") or page.get("requested_url") or "")
                    purpose = str(channel.get("purpose") or "")
                    excerpt = str(channel.get("evidence") or "")
                    if kind in email_types and "@" in email:
                        try:
                            if normalize_domain("https://" + email.rsplit("@", 1)[-1]) != row["canonical_domain"]:
                                continue
                        except ValueError:
                            continue
                        db.execute("INSERT OR IGNORE INTO hotel_contact_channels VALUES(?,?,?,?,?,?,?,?,?)",
                            (row["candidate_id"], kind, email, route, source_url, excerpt, purpose, "PENDING", now))
                        channel_counts["emails"] += 1
                    elif kind == "CONTACT_FORM" and route:
                        try:
                            if normalize_domain(route) != row["canonical_domain"] or normalize_domain(source_url) != row["canonical_domain"]:
                                continue
                        except ValueError:
                            continue
                        db.execute("INSERT OR IGNORE INTO hotel_contact_channels VALUES(?,?,?,?,?,?,?,?,?)",
                            (row["candidate_id"], kind, "", route, source_url, excerpt, purpose, "PENDING", now))
                        channel_counts["forms"] += 1
            booking_terms = ("book now", "reserve now", "check availability", "book a room", "make a reservation", "availability")
            operating_page = any(any(term in " ".join(str(page.get(key) or "") for key in ("title", "meta_description")).casefold() for term in booking_terms) for page in service_pages)
            op_status = "OPERATING_SIGNAL_REVIEW_REQUIRED" if operating_page and (signals.get("product_or_service_page") or signals.get("substantial_public_information")) else "SERVICE_CONTENT_ONLY_REVIEW_REQUIRED" if service_pages else "NOT_ESTABLISHED"
            if service_pages:
                evidence["hotel_operating_signals"] = {"status": op_status, "requires_human_confirmation": True, "pages": service_pages}
                db.execute("UPDATE candidates SET operating_evidence_status=?,evidence_json=? WHERE candidate_id=?",
                    (op_status, json.dumps(evidence, sort_keys=True), row["candidate_id"]))
        db.execute("UPDATE candidates SET contact_status='CONTACT_FOUND' WHERE candidate_id IN (SELECT DISTINCT candidate_id FROM hotel_contact_channels)")
        db.commit()
    return dict(channel_counts)


def run_hotel_batch(limit: int = 25, *, database: Path = HOTEL_DB, request_budget: int = 100,
                    time_budget_seconds: int = 1800, concurrency: int = 1,
                    candidate_ids: list[str] | None = None) -> dict[str, Any]:
    init_hotel_db(database)
    started = datetime.now(timezone.utc).isoformat()
    result = verify_candidate_batch(limit=limit, candidate_db=database, request_budget=request_budget,
        time_budget_seconds=time_budget_seconds, include_contact_evidence=True,
        request_timeout_seconds=15, retry_count=1, delay_between_requests_seconds=1,
        max_concurrency=concurrency, check_sitemap=False, candidate_ids=candidate_ids)
    result["persisted_contact_evidence"] = _persist_hotel_evidence(database)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO hotel_runs(started_at,finished_at,attempted,requests,counts_json,report_json) VALUES(?,?,?,?,?,?)",
            (started, datetime.now(timezone.utc).isoformat(), result["attempted"], result["requests_attempted"],
             json.dumps(result.get("counts", {}), sort_keys=True), json.dumps(result, sort_keys=True)))
        db.commit()
    return result


def select_hotel_pilot(database: Path = HOTEL_DB, limit: int = 25) -> list[str]:
    """Choose a deterministic, domain-diverse sample, suppressing denied domains.

    A 403/429 or robots denial is domain-scoped evidence: querying another hotel
    property on that same host is unlikely to improve identity evidence and can
    aggravate site-side rate limits. Such domains remain in the candidate pool
    and review exports, but are not automatically retried by this worker.
    """
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        blocked_rows = [dict(row) for row in db.execute(
            """SELECT canonical_domain,evidence_json FROM candidates
               WHERE verification_status IN ('ACCESS_BLOCKED','ROBOTS_DENIED')""")]
        rows = [dict(row) for row in db.execute(
            """SELECT candidate_id,canonical_domain,location,chain_status FROM candidates
               WHERE verification_status='DISCOVERED_UNVERIFIED'
               ORDER BY location COLLATE NOCASE,candidate_id""")]
    denied_hosts: set[str] = set()
    for row in blocked_rows:
        domain = str(row.get("canonical_domain") or "").casefold().rstrip(".")
        if domain:
            denied_hosts.add(domain)
        try:
            evidence = json.loads(row.get("evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        for page in evidence.get("pages", []) if isinstance(evidence, dict) else []:
            final_url = page.get("final_url") if isinstance(page, dict) else ""
            if final_url:
                try:
                    denied_hosts.add(normalize_domain(final_url))
                except ValueError:
                    continue

    def host_is_denied(host: str) -> bool:
        host = str(host or "").casefold().rstrip(".")
        return bool(host) and any(host == denied or host.endswith("." + denied) for denied in denied_hosts)

    rows = [row for row in rows if not host_is_denied(row["canonical_domain"])]
    buckets = {
        value: [row for row in rows if row["chain_status"] == value]
        for value in ("CHAIN_INDICATED", "INDEPENDENT_OR_UNKNOWN")
    }
    selected: list[str] = []
    domains: set[str] = set()
    while len(selected) < limit and any(buckets.values()):
        # Independent/unknown properties verified at 3.5x the chain-indicated
        # rate in the first 1,000 inspections (38.9% vs 11.2%). Preserve some
        # chain representation while allocating three slots per independent.
        for value in ("INDEPENDENT_OR_UNKNOWN", "INDEPENDENT_OR_UNKNOWN",
                      "INDEPENDENT_OR_UNKNOWN", "CHAIN_INDICATED"):
            while buckets[value]:
                row = buckets[value].pop(0)
                domain = row["canonical_domain"]
                if domain and domain not in domains:
                    domains.add(domain)
                    selected.append(row["candidate_id"])
                    break
            if len(selected) >= limit:
                break
    return selected


EXPORT_FIELDS = ["candidate_id", "property_name", "official_website", "canonical_domain", "property_type",
                 "candidate_classifications", "hotel_cohort_member", "smb_cohort_member",
                 "hotel_category", "address", "location", "chain_name", "chain_status", "amenities_json",
                 "room_evidence_json", "guest_segments_json", "source_name", "source_url", "source_record_id",
                 "source_license_or_permission", "verification_status", "geo_eligibility_status",
                 "geo_opportunity_status", "contact_status", "human_review_status", "outreach_ready_status",
                 "operating_evidence_status", "verification_reason", "qualification_evidence", "evidence_json", "last_inspected_at"]


def export_hotels(database: Path = HOTEL_DB, output: Path = HOTEL_EXPORT) -> Path:
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM candidates ORDER BY location COLLATE NOCASE,company_name COLLATE NOCASE,candidate_id").fetchall()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS, lineterminator="\r\n")
        writer.writeheader()
        for row in rows:
            data = dict(row)
            mapped = {"candidate_id": data["candidate_id"], "property_name": data["company_name"],
                "official_website": data["website"], "canonical_domain": data["canonical_domain"],
                "property_type": data["property_type"], "candidate_classifications": "BUSINESS;HOTEL",
                "hotel_cohort_member": "YES", "smb_cohort_member": "NO_BY_DEFAULT",
                "hotel_category": data["industry"],
                **{key: data.get(key, "") for key in EXPORT_FIELDS if key not in {"candidate_id", "property_name", "official_website", "canonical_domain", "property_type", "candidate_classifications", "hotel_cohort_member", "smb_cohort_member", "hotel_category"}}}
            writer.writerow({key: _sheet_safe(value) for key, value in mapped.items()})
    temp.replace(output)
    return output


def export_hotel_cohort_membership(database: Path = HOTEL_DB,
                                   output: Path = ROOT / "data/phase7_hotel_cohort_membership.csv") -> Path:
    """Export explicit candidate classifications without merging property/company identities."""
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT candidate_id,company_name,website,canonical_domain,location,address,
            chain_name,chain_status,verification_status,geo_eligibility_status,source_name,source_url,
            source_record_id,source_license_or_permission,human_review_status,outreach_ready_status
            FROM candidates ORDER BY location COLLATE NOCASE,company_name COLLATE NOCASE,candidate_id""").fetchall()
    fields = ["candidate_id", "property_name", "official_website", "canonical_domain", "location", "address",
        "chain_name", "chain_status", "candidate_classifications", "hotel_cohort_member", "smb_cohort_member",
        "verification_status", "geo_eligibility_status", "source_name", "source_url", "source_record_id",
        "source_license_or_permission", "human_review_status", "outreach_ready_status", "identity_link_status"]
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\r\n")
        writer.writeheader()
        for raw in rows:
            row = dict(raw)
            mapped = {"property_name": row["company_name"], "official_website": row["website"],
                "candidate_classifications": "BUSINESS;HOTEL", "hotel_cohort_member": "YES",
                "smb_cohort_member": "NO_BY_DEFAULT", "identity_link_status": "PROPERTY_RECORD;COMPANY_LINK_NOT_ASSERTED"}
            mapped.update({key: row.get(key, "") for key in fields if key not in mapped})
            writer.writerow({key: _sheet_safe(mapped.get(key, "")) for key in fields})
    temp.replace(output)
    return output


def _sheet_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_hotel_views(database: Path = HOTEL_DB) -> dict[str, str]:
    all_path = export_hotels(database)
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM candidates ORDER BY location COLLATE NOCASE,company_name COLLATE NOCASE,candidate_id")]
        channels = [dict(row) for row in db.execute("""SELECT h.candidate_id,h.channel_type,h.email,h.channel_url,h.source_url AS contact_evidence_url,h.evidence,h.purpose,h.review_status,h.discovered_at,
            c.company_name,c.website,c.canonical_domain,c.source_name,c.source_url,c.source_record_id,c.source_license_or_permission,c.verification_status,c.geo_eligibility_status,c.geo_opportunity_status
            FROM hotel_contact_channels h JOIN candidates c USING(candidate_id) WHERE c.verification_status='VERIFIED' AND c.geo_eligibility_status='ELIGIBLE'
            ORDER BY c.location COLLATE NOCASE,c.company_name COLLATE NOCASE,h.channel_type,h.email,h.channel_url""")]
    def write_rows(path: Path, fields: list[str], values: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows({key: _sheet_safe(row.get(key)) for key in fields} for row in values)
        temp.replace(path)
    verified = [row for row in rows if row["verification_status"] == "VERIFIED"]
    eligible = [row for row in verified if row["geo_eligibility_status"] == "ELIGIBLE"]
    failed = [row for row in rows if row["verification_status"] not in {"DISCOVERED_UNVERIFIED", "MISSING_OFFICIAL_WEBSITE", "VERIFIED"}]
    contact_fields = ["candidate_id", "company_name", "website", "canonical_domain", "source_name", "source_url",
                      "source_record_id", "source_license_or_permission", "verification_status", "geo_eligibility_status",
                      "geo_opportunity_status", "channel_type", "email", "channel_url", "contact_evidence_url", "evidence", "purpose", "review_status", "discovered_at"]
    write_rows(HOTEL_VERIFIED_EXPORT, EXPORT_FIELDS, verified)
    write_rows(HOTEL_ELIGIBLE_EXPORT, EXPORT_FIELDS, eligible)
    write_rows(HOTEL_CONTACT_EXPORT, contact_fields, channels)
    write_rows(HOTEL_FAILURE_EXPORT, EXPORT_FIELDS, failed)
    return {"all_candidates": str(all_path), "verified_properties": str(HOTEL_VERIFIED_EXPORT),
            "geo_eligible_properties": str(HOTEL_ELIGIBLE_EXPORT), "contact_candidates": str(HOTEL_CONTACT_EXPORT),
            "validation_failures": str(HOTEL_FAILURE_EXPORT)}


def export_geo_platform_profiles(database: Path = HOTEL_DB, output: Path = GEO_PROFILE_EXPORT) -> Path:
    """Export evidence-backed, verified hotel properties for the GEO platform's audit input.

    This is a one-way data contract; it does not create platform properties, run audits,
    mutate company websites, or authorize outreach.
    """
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        properties = db.execute(
            """SELECT * FROM candidates WHERE verification_status='VERIFIED'
               AND geo_eligibility_status='ELIGIBLE'
               ORDER BY canonical_domain COLLATE NOCASE,candidate_id"""
        ).fetchall()
        channels = db.execute(
            """SELECT candidate_id,channel_type,channel_url,source_url,review_status
               FROM hotel_contact_channels WHERE review_status<>'REJECTED'
               ORDER BY candidate_id,channel_type,channel_url"""
        ).fetchall()
    by_candidate: dict[str, list[dict[str, str]]] = {}
    for channel in channels:
        by_candidate.setdefault(channel["candidate_id"], []).append({
            "type": channel["channel_type"], "url": channel["channel_url"],
            "evidence_url": channel["source_url"], "review_status": channel["review_status"],
        })
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in properties:
            evidence = json.loads(row["evidence_json"] or "{}")
            page_evidence = [{
                "url": page.get("final_url") or page.get("requested_url"),
                "title": page.get("title", ""), "description": page.get("meta_description", ""),
                "http_status": page.get("http_status"), "content_hash": page.get("content_hash"),
            } for page in evidence.get("pages", [])]
            record = {
                "schema_version": "geo-company-profile/v1",
                "profile_id": row["candidate_id"],
                "profile_type": "hotel_property",
                "classifications": ["BUSINESS", "HOTEL"],
                "cohort_membership": {"hotel": True, "smb": False,
                    "smb_exclusion_reason": "Hotels are excluded from the 5,000-SMB target by default; size evidence and explicit approval would be required to count one in both cohorts."},
                "identity": {"name": row["company_name"], "website": row["website"],
                    "canonical_domain": row["canonical_domain"], "verification_status": row["verification_status"],
                    "verification_evidence": row["verification_reason"]},
                "hotel": {"category": row["industry"], "address": row["address"],
                    "location": row["location"], "chain_name": row["chain_name"],
                    "chain_status": row["chain_status"], "amenities": json.loads(row["amenities_json"] or "{}"),
                    "room_evidence": json.loads(row["room_evidence_json"] or "[]"),
                    "guest_segments": json.loads(row["guest_segments_json"] or "[]"),
                    "operating_evidence_status": row["operating_evidence_status"]},
                "qualification": {"eligibility": row["geo_eligibility_status"],
                    "opportunity": row["geo_opportunity_status"],
                    "evidence": json.loads(row["qualification_evidence"] or "{}")},
                "website_evidence": page_evidence,
                "provenance": {"source_name": row["source_name"], "source_url": row["source_url"],
                    "source_record_id": row["source_record_id"],
                    "license": row["source_license_or_permission"], "discovered_at": row["discovery_timestamp"],
                    "inspected_at": row["last_inspected_at"]},
                "contact_channels": by_candidate.get(row["candidate_id"], []),
                "review": {"human_review_status": row["human_review_status"],
                    "outreach_ready_status": row["outreach_ready_status"],
                    "website_changes_applied": False},
            }
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    temp.replace(output)
    return output


def export_startup_geo_profiles(database: Path = STARTUP_PROFILE_DB,
                                output: Path = STARTUP_PROFILE_EXPORT) -> Path:
    """Export only verified, eligible YC-attributed company profiles; never infer startup status from OSM."""
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT * FROM candidates WHERE source_name LIKE 'Y Combinator %'
               AND verification_status='VERIFIED' AND geo_eligibility_status='ELIGIBLE'
               ORDER BY canonical_domain COLLATE NOCASE,candidate_id"""
        ).fetchall()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            evidence = json.loads(row["evidence_json"] or "{}")
            page_evidence = [{"url": page.get("final_url") or page.get("requested_url"),
                "title": page.get("title", ""), "description": page.get("meta_description", ""),
                "http_status": page.get("http_status"), "content_hash": page.get("content_hash")}
                for page in evidence.get("pages", [])]
            record = {"schema_version": "geo-company-profile/v1", "profile_id": row["candidate_id"],
                "profile_type": "startup_company",
                "identity": {"name": row["company_name"], "website": row["website"],
                    "canonical_domain": row["canonical_domain"], "verification_status": row["verification_status"],
                    "verification_evidence": row["verification_reason"]},
                "startup": {"source_attribution": row["source_name"], "industry": row["industry"],
                    "location": row["location"], "employee_count": None, "size_evidence_status": "SIZE_UNKNOWN"},
                "qualification": {"eligibility": row["geo_eligibility_status"],
                    "opportunity": row["geo_opportunity_status"],
                    "evidence": json.loads(row["qualification_evidence"] or "{}")},
                "website_evidence": page_evidence,
                "provenance": {"source_name": row["source_name"], "source_url": row["source_url"],
                    "source_record_id": row["source_record_id"],
                    "license_or_permission": row["source_license_or_permission"],
                    "discovered_at": row["discovery_timestamp"], "inspected_at": row["last_inspected_at"]},
                "review": {"human_review_status": row["human_review_status"],
                    "outreach_ready_status": row["outreach_ready_status"], "website_changes_applied": False}}
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    temp.replace(output)
    return output
