"""Checkpointed hotel prospect promotion for scheduled discovery runs.

The existing Phase 7 worker remains the verifier.  This module is the small
durable control plane used by a Render cron job: it consumes either a bounded
public Overpass source page or an optional source manifest, applies the same
conservative gates, and promotes only qualified US hotel properties into the
lean registry. Raw source rows are never stored in the operational database.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx

from outreach_agent.database import connect_database, is_postgres_target
from outreach_agent.geographic_eligibility import VERIFIED_US, classify_geography
from outreach_agent.hotels import init_hotel_db, run_hotel_batch
from outreach_agent.lean_registry import init_lean_registry
from outreach_agent.normalization import normalize_domain
from outreach_agent.research_lifecycle import PROFILE_VERSION, register_profile, now

DEFAULT_TARGET = 1000
DEFAULT_BATCH_SIZE = 25
DEFAULT_MAX_SOURCE_BYTES = 5_000_000
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_REGIONS = (
    ("boston", 42.20, -71.30, 42.60, -70.80),
    ("new-york", 40.45, -74.30, 41.05, -73.60),
    ("washington-dc", 38.70, -77.30, 39.20, -76.80),
    ("chicago", 41.55, -88.00, 42.15, -87.40),
    ("miami", 25.55, -80.50, 26.40, -80.00),
    ("new-orleans", 29.70, -90.30, 30.20, -89.70),
    ("austin", 30.00, -98.10, 30.70, -97.30),
    ("denver", 39.45, -105.20, 40.20, -104.50),
    ("phoenix", 33.20, -112.50, 33.90, -111.70),
    ("los-angeles", 33.60, -118.90, 34.40, -117.60),
    ("san-francisco", 37.55, -122.70, 38.20, -121.80),
    ("seattle", 47.20, -122.60, 47.90, -121.80),
)
ACTIONABLE_EMAIL_TYPES = {
    "GENERAL_BUSINESS_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL",
    "PARTNERSHIP_EMAIL", "SALES_MARKETING_EMAIL",
}


def _connect(target: str | Path):
    db = connect_database(target)
    if not is_postgres_target(target):
        db.row_factory = sqlite3.Row
    return db


def _table(target: str | Path) -> None:
    init_lean_registry(target)
    with _connect(target) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hotel_discovery_jobs (
              job_id TEXT PRIMARY KEY, status TEXT NOT NULL, target_count INTEGER NOT NULL,
              starting_count INTEGER NOT NULL DEFAULT 0, scanned_count INTEGER NOT NULL DEFAULT 0,
              promoted_count INTEGER NOT NULL DEFAULT 0, rejected_count INTEGER NOT NULL DEFAULT 0,
              duplicate_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0,
              source_cursor TEXT NOT NULL DEFAULT '', last_run_at TEXT, last_success_at TEXT,
              paused_reason TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hotel_discovery_events (
              event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source_record_id TEXT NOT NULL,
              property_identity TEXT NOT NULL, event_type TEXT NOT NULL, detail_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(job_id, source_record_id, event_type)
            );
            CREATE INDEX IF NOT EXISTS hotel_discovery_event_job_idx
              ON hotel_discovery_events(job_id, created_at);
            CREATE TABLE IF NOT EXISTS hotel_discovery_source_stats (
              source_name TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0,
              rows_discovered INTEGER NOT NULL DEFAULT 0, rows_verified INTEGER NOT NULL DEFAULT 0,
              rows_promoted INTEGER NOT NULL DEFAULT 0, rows_duplicate INTEGER NOT NULL DEFAULT 0,
              rows_rejected INTEGER NOT NULL DEFAULT 0, rows_errors INTEGER NOT NULL DEFAULT 0,
              last_run_at TEXT, last_error TEXT
            );
            """
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(hotel_discovery_jobs)")}
        if "source_mode" not in columns:
            db.execute("ALTER TABLE hotel_discovery_jobs ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'MANIFEST'")


def _url(value: str) -> str:
    return value.strip() if value else ""


def _identity(row: dict[str, Any]) -> str:
    # The physical address is intentionally part of identity: chain properties
    # can share a website and must remain separate records.
    parts = [str(row.get(key) or "").strip().casefold() for key in
             ("property_name", "address", "location", "state", "postal_code")]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _checksum(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _records_from_bytes(raw: bytes, content_type: str = "") -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    if isinstance(value, dict):
        value = value.get("records") or value.get("properties") or []
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("hotel source must be a JSON array/object or CSV with headers")
    return [dict(row) for row in value]


def load_source(source: str | Path, *, max_bytes: int = DEFAULT_MAX_SOURCE_BYTES) -> list[dict[str, Any]]:
    """Load one bounded public source manifest; HTTPS is required for URLs."""
    source_text = str(source)
    if source_text.startswith(("http://", "https://")):
        if not source_text.startswith("https://"):
            raise ValueError("hotel discovery sources must use HTTPS")
        response = httpx.get(source_text, timeout=20, follow_redirects=True,
                             headers={"User-Agent": "geo-research-outreach-agent/1.0 hotel discovery"})
        response.raise_for_status()
        raw = response.content
    else:
        raw = Path(source).read_bytes()
    if len(raw) > max_bytes:
        raise ValueError(f"hotel discovery source exceeds {max_bytes} byte bound")
    return _records_from_bytes(raw)


def discover_overpass_source(cursor: int = 0, *, client: httpx.Client | None = None,
                             max_elements: int = 100) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Fetch one bounded US region from the public Overpass OSM service.

    OSM data is public and ODbL-licensed.  One region is requested per job
    pass, with a small result cap and a descriptive User-Agent.  The caller is
    responsible for scheduling a conservative interval between requests.
    """
    if cursor >= len(OVERPASS_REGIONS):
        return [], {"source_name": "OpenStreetMap Overpass", "exhausted": True}, cursor
    name, south, west, north, east = OVERPASS_REGIONS[cursor]
    query = f"[out:json][timeout:25];nwr[\"tourism\"=\"hotel\"]({south},{west},{north},{east});out center tags {max_elements};"
    own_client = client is None
    client = client or httpx.Client(timeout=float(os.environ.get("HOTEL_DISCOVERY_OVERPASS_TIMEOUT", "15")), follow_redirects=True, headers={
        "User-Agent": "geo-research-outreach-agent/1.0 (bounded hotel discovery; OSM attribution)"})
    response = client.post(OVERPASS_URL, data={"data": query})
    if own_client:
        client.close()
    response.raise_for_status()
    body = response.json()
    rows: list[dict[str, Any]] = []
    for element in body.get("elements", [])[:max_elements]:
        tags = element.get("tags") or {}
        name_value = str(tags.get("name") or tags.get("brand") or "").strip()
        if not name_value:
            continue
        element_type = str(element.get("type") or "nwr")
        element_id = str(element.get("id") or "")
        source_id = f"{element_type}/{element_id}"
        address_parts = [tags.get(key, "") for key in ("addr:housenumber", "addr:street", "addr:city", "addr:state", "addr:postcode")]
        address = ", ".join(str(part).strip() for part in address_parts if str(part).strip())
        city = str(tags.get("addr:city") or "")
        state = str(tags.get("addr:state") or "")
        location = ", ".join(part for part in (city, state, "United States") if part)
        website = str(tags.get("contact:website") or tags.get("website") or "").strip()
        if website and "://" not in website:
            website = "https://" + website
        try:
            domain = normalize_domain(website) if website else ""
        except ValueError:
            domain = ""
        email = str(tags.get("contact:email") or tags.get("email") or "").strip()
        chain_name = str(tags.get("brand") or tags.get("operator") or "").strip()
        channels = []
        if email and "@" in email:
            channels.append({"channel_type": "GENERAL_BUSINESS_EMAIL", "email": email,
                             "source_url": website or OVERPASS_URL,
                             "evidence": "Public contact email tagged on OSM property record"})
        rows.append({
            "source_record_id": source_id, "candidate_id": "overpass-hotel:" + hashlib.sha256(source_id.encode()).hexdigest()[:24],
            "property_name": name_value, "address": address, "location": location,
            "official_website": website, "canonical_domain": domain,
            "verification_status": "DISCOVERED_UNVERIFIED", "qualification_status": "UNKNOWN",
            "source_name": f"OpenStreetMap Overpass US region {name}", "source_url": OVERPASS_URL,
            "source_license_or_permission": "OpenStreetMap ODbL 1.0; attribution required",
            "source_batch": name, "discovery_timestamp": now(), "contact_channels": channels,
            "chain_name": chain_name, "preference": "INDEPENDENT_OR_UNKNOWN" if not chain_name else "BRANDED_OR_GROUP",
        })
    rows.sort(key=lambda row: (row.get("preference") != "INDEPENDENT_OR_UNKNOWN", row.get("property_name", "").casefold()))
    return rows, {"source_name": f"OpenStreetMap Overpass US region {name}", "region": name, "query": query, "exhausted": False}, cursor + 1


def discover_nominatim_source(cursor: int = 0, *, client: httpx.Client | None = None,
                              max_elements: int = 25) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Bounded fallback using Nominatim's public regional search endpoint.

    Nominatim is a search service, not a bulk directory. This adapter performs
    at most one query per job pass, uses a descriptive User-Agent, and leaves
    the normal first-party verifier to reject records without an official site.
    Operators must keep the cron interval within Nominatim's usage policy.
    """
    if cursor >= len(OVERPASS_REGIONS):
        return [], {"source_name": "OpenStreetMap Nominatim", "exhausted": True}, cursor
    name, _south, _west, _north, _east = OVERPASS_REGIONS[cursor]
    city = name.replace("-", " ")
    own_client = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True, headers={
        "User-Agent": "geo-research-outreach-agent/1.0 (bounded hotel discovery; contact maintainer)"})
    response = client.get(NOMINATIM_URL, params={
        "format": "jsonv2", "limit": min(max_elements, 25), "extratags": 1,
        "addressdetails": 1, "q": f"hotel {city} USA",
    })
    if own_client:
        client.close()
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    for item in response.json()[:max_elements]:
        address = item.get("display_name") or ""
        address_data = item.get("address") or {}
        country = str(address_data.get("country") or "")
        state = str(address_data.get("state") or "")
        location = ", ".join(part for part in (
            address_data.get("city") or address_data.get("town") or address_data.get("village") or city,
            state, country or "United States") if part)
        tags = item.get("extratags") or {}
        website = str(tags.get("contact:website") or tags.get("website") or "").strip()
        if website and "://" not in website:
            website = "https://" + website
        try:
            domain = normalize_domain(website) if website else ""
        except ValueError:
            domain = ""
        osm_type = str(item.get("osm_type") or "way")
        source_id = f"{osm_type}/{item.get('osm_id')}"
        channels = []
        email = str(tags.get("contact:email") or tags.get("email") or "").strip()
        if email and "@" in email:
            channels.append({"channel_type": "GENERAL_BUSINESS_EMAIL", "email": email,
                             "source_url": website or NOMINATIM_URL,
                             "evidence": "Public contact email tagged on OSM property record"})
        rows.append({
            "source_record_id": source_id,
            "candidate_id": "nominatim-hotel:" + hashlib.sha256(source_id.encode()).hexdigest()[:24],
            "property_name": str(item.get("name") or address.split(",")[0] or source_id),
            "address": address, "location": location, "official_website": website,
            "canonical_domain": domain, "verification_status": "DISCOVERED_UNVERIFIED",
            "qualification_status": "UNKNOWN",
            "source_name": f"OpenStreetMap Nominatim US region {name}",
            "source_url": NOMINATIM_URL,
            "source_license_or_permission": "OpenStreetMap ODbL 1.0; Nominatim usage policy",
            "source_batch": name, "discovery_timestamp": now(),
            "contact_channels": channels, "preference": "INDEPENDENT_OR_UNKNOWN",
        })
    return rows, {"source_name": f"OpenStreetMap Nominatim US region {name}",
                  "region": name, "exhausted": False}, cursor + 1


def discover_public_source(cursor: int = 0, *, max_elements: int = 100) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    """Use Overpass first and fall back to one bounded Nominatim search."""
    try:
        return discover_overpass_source(cursor, max_elements=max_elements)
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        rows, meta, next_cursor = discover_nominatim_source(cursor, max_elements=min(max_elements, 25))
        meta["fallback_from"] = OVERPASS_URL
        meta["fallback_error_type"] = type(exc).__name__
        return rows, meta, next_cursor


def _verify_source_rows(rows: list[dict[str, Any]], *, request_budget: int) -> list[dict[str, Any]]:
    """Run the existing first-party verifier against a temporary raw store."""
    if not rows:
        return []
    with tempfile.TemporaryDirectory(prefix="geo-hotel-discovery-") as directory:
        database = Path(directory) / "candidates.db"
        init_hotel_db(database)
        with sqlite3.connect(database) as db:
            for row in rows:
                db.execute(
                    """INSERT INTO candidates
                    (candidate_id,company_name,website,canonical_domain,industry,location,source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,geo_eligibility_status,geo_opportunity_status,contact_status,human_review_status,outreach_ready_status,verification_reason,qualification_evidence,fetch_status,http_status,evidence_json,last_inspected_at,source_batch,address,chain_name,chain_status,amenities_json,room_evidence_json,guest_segments_json,operating_evidence_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["candidate_id"], row["property_name"], row["official_website"], row["canonical_domain"], "hotel", row["location"], row["source_name"], row["source_url"], row["source_record_id"], row["source_license_or_permission"], row["discovery_timestamp"], row["verification_status"], "UNKNOWN", "UNKNOWN", "NOT_DISCOVERED", "PENDING", "NOT_READY", "", "", "", None, "{}", None, row["source_batch"], row["address"], "", "UNKNOWN", "{}", "[]", "[]", "NOT_ASSESSED"),
                )
            db.commit()
        run_hotel_batch(min(len(rows), 25), database=database, request_budget=request_budget,
                        time_budget_seconds=600, concurrency=1,
                        candidate_ids=[row["candidate_id"] for row in rows[:25]])
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            verified = [dict(row) for row in db.execute("SELECT * FROM candidates")]
            channels = {}
            for row in db.execute("SELECT * FROM hotel_contact_channels"):
                channels.setdefault(row["candidate_id"], []).append(dict(row))
        for row in verified:
            row["official_website"] = row.get("website") or ""
            row["qualification_status"] = row.get("geo_eligibility_status") or "UNKNOWN"
            row["contact_channels"] = channels.get(row["candidate_id"], [])
        return verified


def _channels(row: dict[str, Any]) -> list[dict[str, Any]]:
    value = row.get("contact_channels") or row.get("contacts") or []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _eligible(row: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    blocked = " ".join(str(row.get(key) or "") for key in
                        ("rejection_status", "suppression_status", "withdrawal_status", "conflict_status")).upper()
    if any(term in blocked for term in ("REJECTED", "SUPPRESSED", "WITHDRAWN", "CONFLICT")):
        return False, "BLOCKED_BY_PRIOR_DECISION", {}
    location = str(row.get("location") or row.get("address") or "")
    classification, evidence = classify_geography(
        location=location, address=str(row.get("address") or ""),
        source_name=str(row.get("source_name") or ""),
        source_batch=str(row.get("source_batch") or ""), profile_type="hotel_property")
    if classification != VERIFIED_US:
        return False, classification, evidence
    if str(row.get("verification_status") or "").upper() != "VERIFIED":
        return False, "IDENTITY_NOT_VERIFIED", evidence
    if str(row.get("qualification_status") or row.get("geo_eligibility_status") or "").upper() not in {"ELIGIBLE", "QUALIFIED"}:
        return False, "NOT_GEO_ELIGIBLE", evidence
    if not _url(str(row.get("official_website") or row.get("website") or "")):
        return False, "OFFICIAL_WEBSITE_MISSING", evidence
    channels = [c for c in _channels(row) if str(c.get("review_status") or "PENDING").upper() != "REJECTED"]
    actionable = any(str(c.get("channel_type") or "") == "CONTACT_FORM" and _url(str(c.get("channel_url") or "")) for c in channels)
    actionable = actionable or any(str(c.get("channel_type") or "") in ACTIONABLE_EMAIL_TYPES and "@" in str(c.get("email") or "") for c in channels)
    if not actionable:
        return False, "NO_ACTIONABLE_CONTACT", evidence
    return True, "PROMOTABLE", evidence


def _ensure_job(db, job_id: str, target_count: int, source_mode: str = "MANIFEST") -> dict[str, Any]:
    row = db.execute("SELECT * FROM hotel_discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row:
        existing = dict(row)
        saved_mode = str(existing.get("source_mode") or "MANIFEST").upper()
        if saved_mode != source_mode and int(existing.get("scanned_count") or 0) > 0:
            raise ValueError(f"job {job_id} already has source_mode={saved_mode}; use a new job_id to change source")
        if saved_mode != source_mode:
            db.execute("UPDATE hotel_discovery_jobs SET source_mode=?,updated_at=? WHERE job_id=?", (source_mode, now(), job_id))
            existing["source_mode"] = source_mode
        return existing
    timestamp = now()
    current = db.execute("SELECT COUNT(*) AS count FROM research_records WHERE profile_type='hotel_property' AND profile_type<>'owner_controlled_test'").fetchone()["count"]
    db.execute("INSERT INTO hotel_discovery_jobs(job_id,status,target_count,starting_count,source_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (job_id, "RUNNABLE", target_count, current, source_mode, timestamp, timestamp))
    return dict(db.execute("SELECT * FROM hotel_discovery_jobs WHERE job_id=?", (job_id,)).fetchone())


def _source_mode(source: str | Path | None, requested: str | None) -> str:
    mode = (requested or os.environ.get("HOTEL_DISCOVERY_SOURCE_MODE", "AUTO")).upper()
    if mode == "AUTO":
        mode = "MANIFEST" if source else "OVERPASS"
    if mode not in {"MANIFEST", "OVERPASS"}:
        raise ValueError("source_mode must be AUTO, MANIFEST, or OVERPASS")
    if mode == "MANIFEST" and not source:
        raise ValueError("a manifest source is required when source_mode=MANIFEST")
    return mode


def _update_source_stats(db, source_name: str, *, discovered: int, verified: int,
                         promoted: int, duplicates: int, rejected: int, errors: int,
                         error: str | None = None) -> None:
    db.execute(
        """INSERT INTO hotel_discovery_source_stats
           (source_name,requests,rows_discovered,rows_verified,rows_promoted,rows_duplicate,rows_rejected,rows_errors,last_run_at,last_error)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_name) DO UPDATE SET
             requests=hotel_discovery_source_stats.requests+1,
             rows_discovered=hotel_discovery_source_stats.rows_discovered+excluded.rows_discovered,
             rows_verified=hotel_discovery_source_stats.rows_verified+excluded.rows_verified,
             rows_promoted=hotel_discovery_source_stats.rows_promoted+excluded.rows_promoted,
             rows_duplicate=hotel_discovery_source_stats.rows_duplicate+excluded.rows_duplicate,
             rows_rejected=hotel_discovery_source_stats.rows_rejected+excluded.rows_rejected,
             rows_errors=hotel_discovery_source_stats.rows_errors+excluded.rows_errors,
             last_run_at=excluded.last_run_at,last_error=excluded.last_error""",
        (source_name, 1, discovered, verified, promoted, duplicates, rejected, errors, now(), error),
    )


def status(target: str | Path, job_id: str = "hotel-prospect-discovery") -> dict[str, Any]:
    _table(target)
    with _connect(target) as db:
        job = db.execute("SELECT * FROM hotel_discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            target_count = int(os.environ.get("HOTEL_PROSPECT_TARGET", DEFAULT_TARGET))
            return {"job_id": job_id, "status": "NOT_INITIALIZED", "target_count": target_count, "qualified_prospects": 0}
        qualified = db.execute("SELECT COUNT(*) AS count FROM research_records WHERE profile_type='hotel_property' AND profile_type<>'owner_controlled_test' AND qualification_status='ELIGIBLE' AND geographic_classification='VERIFIED_US'").fetchone()["count"]
        source_stats = [dict(row) for row in db.execute("SELECT * FROM hotel_discovery_source_stats ORDER BY source_name")]
        return {**dict(job), "qualified_prospects": int(qualified), "remaining": max(0, int(job["target_count"]) - int(qualified)), "source_stats": source_stats}


def pause(target: str | Path, reason: str = "operator_pause", job_id: str = "hotel-prospect-discovery") -> dict[str, Any]:
    _table(target)
    with _connect(target) as db:
        timestamp = now()
        db.execute("UPDATE hotel_discovery_jobs SET status='PAUSED',paused_reason=?,updated_at=? WHERE job_id=?", (reason, timestamp, job_id))
    return status(target, job_id)


def resume(target: str | Path, job_id: str = "hotel-prospect-discovery") -> dict[str, Any]:
    _table(target)
    with _connect(target) as db:
        timestamp = now()
        db.execute("UPDATE hotel_discovery_jobs SET status='RUNNABLE',paused_reason=NULL,last_error=NULL,updated_at=? WHERE job_id=?", (timestamp, job_id))
    return status(target, job_id)


def run_once(target: str | Path, source: str | Path | None = None, *, job_id: str = "hotel-prospect-discovery", target_count: int = DEFAULT_TARGET, batch_size: int = DEFAULT_BATCH_SIZE, source_mode: str | None = None, request_budget: int | None = None) -> dict[str, Any]:
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    mode = _source_mode(source, source_mode)
    _table(target)
    with _connect(target) as db:
        job = _ensure_job(db, job_id, target_count, mode)
        if job["status"] == "PAUSED":
            return {**dict(job), "status": "PAUSED", "processed": 0}
        qualified = int(db.execute("SELECT COUNT(*) AS count FROM research_records WHERE profile_type='hotel_property' AND profile_type<>'owner_controlled_test' AND qualification_status='ELIGIBLE' AND geographic_classification='VERIFIED_US'").fetchone()["count"])
        if qualified >= target_count:
            db.execute("UPDATE hotel_discovery_jobs SET status='TARGET_REACHED',updated_at=? WHERE job_id=?", (now(), job_id))
            return status(target, job_id)
        cursor = int(job.get("source_cursor") or 0)
        source_name = "manifest"
        source_next_cursor = cursor
        if mode == "OVERPASS":
            raw_rows, source_meta, source_next_cursor = discover_public_source(
                cursor, max_elements=max(batch_size, min(100, batch_size * 4)))
            source_name = str(source_meta.get("source_name") or "OpenStreetMap Overpass")
            if source_meta.get("exhausted"):
                db.execute("UPDATE hotel_discovery_jobs SET status='SOURCE_EXHAUSTED',last_run_at=?,updated_at=? WHERE job_id=?", (now(), now(), job_id))
                return {**status(target, job_id), "source_name": source_name, "source_cursor": cursor, "processed": 0}
            source_rows = _verify_source_rows(raw_rows, request_budget=request_budget or int(os.environ.get("HOTEL_DISCOVERY_REQUEST_BUDGET", "25")))
            selected = source_rows
        else:
            rows = load_source(source)  # type: ignore[arg-type]
            selected = rows[cursor:cursor + batch_size]
            source_rows = selected
            if selected:
                source_name = str(selected[0].get("source_name") or "manifest")
            source_next_cursor = cursor + len(selected)
        counts = {"scanned": 0, "promoted": 0, "rejected": 0, "duplicates": 0, "errors": 0}
        processed_rows = 0
        for row in selected:
            if qualified + counts["promoted"] >= target_count:
                break
            processed_rows += 1
            counts["scanned"] += 1
            source_id = str(row.get("source_record_id") or row.get("candidate_id") or "")
            identity = _identity(row)
            try:
                if not source_id:
                    counts["rejected"] += 1
                    continue
                exists = db.execute("SELECT 1 FROM hotel_discovery_events WHERE job_id=? AND source_record_id=? AND event_type='SEEN'", (job_id, source_id)).fetchone()
                if exists:
                    counts["duplicates"] += 1
                    continue
                ok, reason, geo_evidence = _eligible(row)
                if not ok:
                    counts["rejected"] += 1
                    db.execute("INSERT INTO hotel_discovery_events(event_id,job_id,source_record_id,property_identity,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_record_id,event_type) DO NOTHING", ("event-" + secrets.token_urlsafe(10), job_id, source_id, identity, "SEEN", json.dumps({"reason": reason}, sort_keys=True), now()))
                    continue
                suppressed = False
                for channel in _channels(row):
                    email = str(channel.get("email") or "").strip().casefold()
                    if email and db.execute("SELECT 1 FROM suppressed_recipients WHERE lower(recipient)=?", (email,)).fetchone():
                        suppressed = True
                        break
                if suppressed:
                    counts["rejected"] += 1
                    db.execute("INSERT INTO hotel_discovery_events(event_id,job_id,source_record_id,property_identity,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_record_id,event_type) DO NOTHING", ("event-" + secrets.token_urlsafe(10), job_id, source_id, identity, "SEEN", json.dumps({"reason": "SUPPRESSED_RECIPIENT"}, sort_keys=True), now()))
                    continue
                existing_source = db.execute("SELECT profile_id FROM research_records WHERE source_candidate_id=?", (source_id,)).fetchone() if source_id else None
                if existing_source:
                    counts["duplicates"] += 1
                    db.execute("INSERT INTO hotel_discovery_events(event_id,job_id,source_record_id,property_identity,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_record_id,event_type) DO NOTHING", ("event-" + secrets.token_urlsafe(10), job_id, source_id, identity, "SEEN", json.dumps({"reason": "SOURCE_ID_ALREADY_PROMOTED"}, sort_keys=True), now()))
                    continue
                prior = db.execute("SELECT prospect_id FROM prospect_identifiers WHERE identifier_type='HOTEL_PROPERTY_IDENTITY' AND identifier_value=?", (identity,)).fetchone()
                if prior:
                    counts["duplicates"] += 1
                    db.execute("INSERT INTO hotel_discovery_events(event_id,job_id,source_record_id,property_identity,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_record_id,event_type) DO NOTHING", ("event-" + secrets.token_urlsafe(10), job_id, source_id, identity, "SEEN", json.dumps({"reason": "PROPERTY_IDENTITY_ALREADY_PROMOTED"}, sort_keys=True), now()))
                    continue
                profile_id = "hotel-discovery:" + identity[:32]
                profile = {
                    "schema_version": PROFILE_VERSION, "profile_id": profile_id, "profile_type": "hotel_property",
                    "identity": {"name": str(row.get("property_name") or row.get("name") or profile_id), "website": _url(str(row.get("official_website") or row.get("website") or "")), "verification_status": "VERIFIED", "verification_evidence": [geo_evidence]},
                    "hotel": {"location": str(row.get("location") or row.get("address") or ""), "category": "hotel", "chain_name": str(row.get("chain_name") or "")},
                    "qualification": {"eligibility": "ELIGIBLE"},
                    "provenance": {"source_name": str(row.get("source_name") or "public_hotel_source"), "source_record_id": source_id, "source_url": str(row.get("source_url") or ""), "source_batch": str(row.get("source_batch") or "")},
                    "contact_channels": _channels(row), "cohort_membership": {"hotel": True, "smb": False},
                }
                # register_profile uses the same database abstraction but opens
                # its own transaction; release the outer SQLite write lock first.
                db.commit()
                register_profile(profile, target)
                checksum = _checksum(row)
                with _connect(target) as out:
                    out.execute("UPDATE research_records SET promotion_state='PROSPECT',contact_verification_status='CONTACT_REVIEW_PENDING',outreach_ready_status='NOT_READY',source_candidate_id=?,source_row_checksum=?,geographic_classification='VERIFIED_US',geographic_evidence_json=?,updated_at=? WHERE profile_id=?", (source_id, checksum, json.dumps(geo_evidence, sort_keys=True), now(), profile_id))
                    for channel in _channels(row):
                        ctype, email, url = str(channel.get("channel_type") or ""), str(channel.get("email") or ""), str(channel.get("channel_url") or "")
                        if ctype == "CONTACT_FORM" and not url and not email:
                            continue
                        out.execute("INSERT INTO prospect_contacts(contact_id,prospect_id,channel_type,email,channel_url,source_url,evidence,purpose,contact_review_status,email_verification_status,captured_at,source_row_checksum) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(prospect_id,channel_type,email,channel_url) DO NOTHING", ("contact-" + _sha(profile_id + ctype + email + url)[:24], profile_id, ctype, email, url, str(channel.get("source_url") or row.get("source_url") or ""), str(channel.get("evidence") or ""), str(channel.get("purpose") or ""), "CONTACT_REVIEW_PENDING", "NOT_VERIFIED", str(channel.get("captured_at") or now()), checksum))
                    out.execute("INSERT INTO prospect_provenance(provenance_id,prospect_id,source_name,source_record_id,source_url,source_license_or_permission,discovery_timestamp,source_row_checksum) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(prospect_id,source_name,source_record_id) DO NOTHING", ("provenance-" + _sha(profile_id + source_id)[:24], profile_id, str(row.get("source_name") or "public_hotel_source"), source_id, str(row.get("source_url") or ""), str(row.get("source_license_or_permission") or ""), str(row.get("discovery_timestamp") or now()), checksum))
                    out.execute("INSERT INTO prospect_identifiers(identifier_id,prospect_id,identifier_type,identifier_value,reason,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(identifier_type,identifier_value) DO NOTHING", ("identifier-" + _sha(identity)[:24], profile_id, "HOTEL_PROPERTY_IDENTITY", identity, "physical property name/address identity", now()))
                db.execute("INSERT INTO hotel_discovery_events(event_id,job_id,source_record_id,property_identity,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_record_id,event_type) DO NOTHING", ("event-" + secrets.token_urlsafe(10), job_id, source_id, identity, "SEEN", json.dumps({"reason": "PROMOTED"}, sort_keys=True), now()))
                counts["promoted"] += 1
            except Exception as exc:
                counts["errors"] += 1
                db.execute("INSERT INTO hotel_discovery_events(event_id,job_id,source_record_id,property_identity,event_type,detail_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id,source_record_id,event_type) DO NOTHING", ("event-" + secrets.token_urlsafe(10), job_id, source_id, identity, "ERROR", json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, sort_keys=True), now()))
        new_cursor = source_next_cursor if counts["errors"] == 0 else cursor
        timestamp = now()
        total_promoted = int(job["promoted_count"]) + counts["promoted"]
        total_scanned = int(job["scanned_count"]) + counts["scanned"]
        new_status = "TARGET_REACHED" if qualified + counts["promoted"] >= target_count else "RUNNABLE"
        db.execute("UPDATE hotel_discovery_jobs SET status=?,scanned_count=?,promoted_count=?,rejected_count=rejected_count+?,duplicate_count=duplicate_count+?,error_count=error_count+?,source_cursor=?,last_run_at=?,last_success_at=?,last_error=?,updated_at=? WHERE job_id=?", (new_status, total_scanned, total_promoted, counts["rejected"], counts["duplicates"], counts["errors"], str(new_cursor), timestamp, timestamp if counts["errors"] == 0 else job.get("last_success_at"), None if counts["errors"] == 0 else "one or more source rows failed", timestamp, job_id))
        _update_source_stats(db, source_name, discovered=len(source_rows),
                             verified=sum(1 for row in source_rows if str(row.get("verification_status") or "").upper() == "VERIFIED"),
                             promoted=counts["promoted"], duplicates=counts["duplicates"],
                             rejected=counts["rejected"], errors=counts["errors"],
                             error=None if counts["errors"] == 0 else "one or more source rows failed")
    return {"job_id": job_id, "status": new_status, "processed": processed_rows, "source_name": source_name, "source_cursor": new_cursor, **counts, **status(target, job_id)}
