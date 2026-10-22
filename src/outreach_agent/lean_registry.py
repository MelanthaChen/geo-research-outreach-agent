"""Lean production prospect registry.

Discovery SQLite stores are immutable inputs.  This module promotes only
qualified US records with an actionable public email or official form into a
small durable prospect registry.  Contact review is a later gate: promotion
to PROSPECT does not imply a verified email or permission to send.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.database import connect_database
from outreach_agent.geographic_eligibility import VERIFIED_US, classify_geography
from outreach_agent.integration import PROFILE_VERSION
from outreach_agent.research_lifecycle import init_registry

ACTIONABLE_EMAIL_TYPES = {
    "GENERAL_BUSINESS_EMAIL",
    "BUSINESS_DEVELOPMENT_EMAIL",
    "PARTNERSHIP_EMAIL",
    "SALES_MARKETING_EMAIL",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(path: Path):
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def init_lean_registry(target: str | Path) -> str | Path:
    """Create the lean schema with ``research_records`` as the sole lifecycle store."""
    if not (isinstance(target, str) and target.startswith(("postgres://", "postgresql://"))):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    init_registry(target)
    db = connect_database(target)
    try:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS prospect_contacts (
              contact_id TEXT PRIMARY KEY,
              prospect_id TEXT NOT NULL,
              channel_type TEXT NOT NULL,
              email TEXT,
              channel_url TEXT,
              source_url TEXT,
              evidence TEXT,
              purpose TEXT,
              contact_review_status TEXT NOT NULL DEFAULT 'CONTACT_REVIEW_PENDING',
              email_verification_status TEXT NOT NULL DEFAULT 'NOT_VERIFIED',
              captured_at TEXT,
              source_row_checksum TEXT NOT NULL,
              FOREIGN KEY(prospect_id) REFERENCES research_records(profile_id),
              UNIQUE(prospect_id, channel_type, email, channel_url)
            );
            CREATE TABLE IF NOT EXISTS prospect_provenance (
              provenance_id TEXT PRIMARY KEY,
              prospect_id TEXT NOT NULL,
              source_name TEXT,
              source_record_id TEXT,
              source_url TEXT,
              source_license_or_permission TEXT,
              discovery_timestamp TEXT,
              source_row_checksum TEXT NOT NULL,
              FOREIGN KEY(prospect_id) REFERENCES research_records(profile_id),
              UNIQUE(prospect_id, source_name, source_record_id)
            );
            CREATE TABLE IF NOT EXISTS prospect_identifiers (
              identifier_id TEXT PRIMARY KEY,
              prospect_id TEXT,
              identifier_type TEXT NOT NULL,
              identifier_value TEXT NOT NULL,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(prospect_id) REFERENCES research_records(profile_id),
              UNIQUE(identifier_type, identifier_value)
            );
            CREATE TABLE IF NOT EXISTS prospect_lifecycle_events (
              event_id TEXT PRIMARY KEY,
              prospect_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              event_value TEXT,
              event_at TEXT NOT NULL,
              FOREIGN KEY(prospect_id) REFERENCES research_records(profile_id),
              UNIQUE(prospect_id, event_type, event_value)
            );
            CREATE INDEX IF NOT EXISTS lean_prospect_state_idx ON research_records(recruitment_status, contact_verification_status, profile_type);
            CREATE INDEX IF NOT EXISTS lean_prospect_geo_idx ON research_records(geographic_classification, qualification_status);
            CREATE INDEX IF NOT EXISTS lean_contact_type_idx ON prospect_contacts(channel_type, email_verification_status);
            """
        )
        db.commit()
        return target
    finally:
        db.close()


def _table_columns(source, table: str) -> set[str]:
    return {row[1] for row in source.execute(f"PRAGMA table_info({table})")}


def _actionable(row: dict[str, Any]) -> bool:
    review = str(row.get("review_status") or "PENDING").upper()
    if review == "REJECTED":
        return False
    channel_type = str(row.get("channel_type") or "")
    if channel_type == "CONTACT_FORM":
        return bool(str(row.get("channel_url") or "").strip())
    return channel_type in ACTIONABLE_EMAIL_TYPES and bool(str(row.get("email") or "").strip())


def _candidate_channels(source, table: str, candidate_id: str) -> list[dict[str, Any]]:
    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    rows = []
    for row in source.execute(f"SELECT * FROM {table} WHERE candidate_id=?", (candidate_id,)):
        item = dict(row)
        if _actionable(item):
            rows.append(item)
    return rows


def _upsert(db, table: str, row: dict[str, Any], key: str) -> None:
    columns = list(row)
    values = [row[column] for column in columns]
    placeholders = ",".join("?" for _ in columns)
    if table in {"prospect_contacts", "prospect_provenance", "prospect_lifecycle_events"}:
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT ({key}) DO NOTHING",
            values,
        )
        return
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != key)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}", values
    )


def _copy_owner_fixture(source_path: str | Path, target) -> int:
    """Copy only owner-controlled lifecycle rows from an isolated fixture DB."""
    source = _source(Path(source_path))
    copied = 0
    try:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "research_records" not in tables:
            return 0
        profile_ids = [row[0] for row in source.execute("SELECT profile_id FROM research_records WHERE profile_type='owner_controlled_test'")]
        invitation_ids: set[str] = set()
        draft_ids: set[str] = set()
        handoff_ids: set[str] = set()
        for table, condition, values in [("research_records", "profile_id=?", profile_ids)]:
            if table not in tables:
                continue
            for profile_id in values:
                row = source.execute(f"SELECT * FROM {table} WHERE {condition}", (profile_id,)).fetchone()
                if not row:
                    continue
                columns = [item[1] for item in target.execute(f"PRAGMA table_info({table})").fetchall()]
                payload = {key: row[key] for key in row.keys() if key in columns}
                _upsert(target, table, payload, "profile_id")
                copied += 1
        if "questionnaire_invitations" in tables:
            rows = [dict(row) for row in source.execute("SELECT * FROM questionnaire_invitations WHERE profile_id IN (SELECT profile_id FROM research_records WHERE profile_type='owner_controlled_test')")]
            invitation_ids = {str(row["invitation_id"]) for row in rows}
            _copy_rows(source, target, "questionnaire_invitations", rows, key="invitation_id")
        if "email_drafts" in tables:
            rows = [dict(row) for row in source.execute("SELECT * FROM email_drafts WHERE profile_id IN (SELECT profile_id FROM research_records WHERE profile_type='owner_controlled_test')")]
            draft_ids = {str(row["draft_id"]) for row in rows}
            _copy_rows(source, target, "email_drafts", rows, key="draft_id")
        for table in ("screenings", "screening_analysis_results", "replies", "geo_handoffs", "consent_reviews"):
            if table in tables:
                rows = [dict(row) for row in source.execute(f"SELECT * FROM {table} WHERE profile_id IN (SELECT profile_id FROM research_records WHERE profile_type='owner_controlled_test')")]
                _copy_rows(source, target, table, rows, key=None)
                if table == "geo_handoffs":
                    handoff_ids = {str(row["handoff_id"]) for row in rows}
        if "questionnaire_submissions" in tables and invitation_ids:
            rows = [dict(row) for row in source.execute("SELECT * FROM questionnaire_submissions WHERE invitation_id IN ({})".format(",".join("?" for _ in invitation_ids)), tuple(invitation_ids))]
            _copy_rows(source, target, "questionnaire_submissions", rows, key="submission_id")
        for table, ids, column, key in (("email_events", draft_ids, "draft_id", "event_id"), ("dispatch_attempts", draft_ids, "draft_id", "attempt_id"), ("geo_handoff_attempts", handoff_ids, "handoff_id", "attempt_id")):
            if table in tables and ids:
                rows = [dict(row) for row in source.execute(f"SELECT * FROM {table} WHERE {column} IN ({','.join('?' for _ in ids)})", tuple(ids))]
                _copy_rows(source, target, table, rows, key=key)
    finally:
        source.close()
    return copied


def _copy_rows(source, target, table: str, rows: list[dict[str, Any]], key: str | None) -> None:
    if not rows:
        return
    target_columns = {item[1] for item in target.execute(f"PRAGMA table_info({table})").fetchall()}
    for row in rows:
        payload = {column: value for column, value in row.items() if column in target_columns}
        if key and key in payload:
            _upsert(target, table, payload, key)
        else:
            columns = list(payload)
            values = [payload[column] for column in columns]
            target.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT DO NOTHING", values)


def promote_sources(*, business_db: Path, hotel_db: Path, target: str | Path, existing_registry: str | Path | None = None) -> dict[str, Any]:
    """Promote qualified/actionable source rows into an isolated lean target.

    The function never writes to either source database.  It is rerunnable and
    keeps prospects in CONTACT_REVIEW_PENDING until a reviewer acts.
    """
    init_lean_registry(target)
    db = connect_database(target)
    if isinstance(db, sqlite3.Connection):
        db.row_factory = sqlite3.Row
    now = "lean-import"
    report: dict[str, Any] = {"schema_version": "lean-outreach-registry/v1", "companies": {}, "hotels": {}, "owner_controlled_records_copied": 0, "totals": {"prospects": 0, "contacts": 0}}
    prior_ids: set[str] = set()
    suppressed: set[str] = set()
    if existing_registry:
        if not (isinstance(existing_registry, str) and existing_registry.startswith(("postgres://", "postgresql://"))):
            report["owner_controlled_records_copied"] = _copy_owner_fixture(existing_registry, db)
        prior = connect_database(existing_registry)
        try:
            for row in prior.execute("SELECT profile_id FROM questionnaire_invitations").fetchall():
                prior_ids.add(str(row[0]))
            for row in prior.execute("SELECT profile_id FROM email_drafts").fetchall():
                prior_ids.add(str(row[0]))
            for row in prior.execute("SELECT recipient FROM suppressed_recipients").fetchall():
                suppressed.add(str(row[0]).casefold())
        finally:
            prior.close()

    def process(path: Path, *, hotel: bool) -> None:
        source = _source(path)
        candidate_table = "hotel_contact_channels" if hotel else "phase6f_contact_channels"
        cohort = "HOTEL" if hotel else "COMPANY"
        counts = {"discovered": 0, "qualified_actionable": 0, "form_only": 0, "email_only": 0, "email_and_form": 0, "contacts": 0, "blocked_prior_contact": 0}
        try:
            for candidate_row in source.execute("SELECT * FROM candidates ORDER BY candidate_id"):
                candidate = dict(candidate_row)
                counts["discovered"] += 1
                candidate_id = str(candidate["candidate_id"])
                address = candidate.get("address") if hotel else None
                geo, _evidence = classify_geography(location=candidate.get("location"), address=address, source_name=candidate.get("source_name"), source_batch=candidate.get("source_batch"), profile_type="hotel_property" if hotel else "business_company")
                if (candidate.get("verification_status") != "VERIFIED" or candidate.get("geo_eligibility_status") != "ELIGIBLE" or geo != VERIFIED_US or not str(candidate.get("website") or "").strip()):
                    continue
                channels = _candidate_channels(source, candidate_table, candidate_id)
                if not channels:
                    continue
                counts["qualified_actionable"] += 1
                has_form = any(str(item.get("channel_type")) == "CONTACT_FORM" for item in channels)
                has_email = any(str(item.get("channel_type")) in ACTIONABLE_EMAIL_TYPES for item in channels)
                if has_form and has_email:
                    counts["email_and_form"] += 1
                elif has_form:
                    counts["form_only"] += 1
                else:
                    counts["email_only"] += 1
                # Keep the source candidate ID as the operational profile ID so
                # existing invitations, replies, consent, and handoff services
                # continue to address the same canonical record.
                prospect_id = candidate_id
                has_suppressed_contact = any(str(item.get("email") or "").strip().casefold() in suppressed for item in channels)
                prior_contact = "CONFLICTING_PRIOR_CONTACT" if candidate_id in prior_ids else "NONE"
                state = "BLOCKED" if prior_contact != "NONE" or has_suppressed_contact else "PROSPECT"
                if state == "BLOCKED":
                    counts["blocked_prior_contact"] += 1
                raw = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
                existing = db.execute(
                    "SELECT created_at,recruitment_status,questionnaire_status,consent_status,geo_research_status,monitoring_status,source_row_checksum FROM research_records WHERE profile_id=?",
                    (prospect_id,),
                ).fetchone()
                lifecycle = {
                    "profile_id": prospect_id, "profile_version": PROFILE_VERSION,
                    "profile_type": "hotel_property" if hotel else "business_company",
                    "name": candidate.get("company_name") or candidate_id,
                    "official_website": candidate.get("website") or "",
                    "industry": candidate.get("industry") or "",
                    "location": candidate.get("location") or "",
                    "size_status": "SIZE_UNKNOWN", "size_evidence_json": "[]",
                    "verification_evidence_json": _json([{"url": candidate.get("source_url"), "reason": candidate.get("verification_reason"), "status": candidate.get("verification_status")}]),
                    "provenance_json": _json([{"source_name": candidate.get("source_name"), "source_url": candidate.get("source_url"), "source_record_id": candidate.get("source_record_id"), "observed_at": candidate.get("discovery_timestamp")}]),
                    "contact_channels_json": _json(channels),
                    "cohort_membership_json": _json({"cohort": cohort, "source_candidate_id": candidate_id}),
                    "identity_verification_status": candidate.get("verification_status") or "UNKNOWN",
                    "geographic_classification": geo,
                    "geographic_evidence_json": _json({"rule": "lean_promotion", "location": candidate.get("location") or "", "address": address or "", "profile_type": "hotel_property" if hotel else "business_company"}),
                    "contact_verification_status": "CONTACT_REVIEW_PENDING",
                    "screening_status": "NOT_SCREENED",
                    "recruitment_priority": candidate.get("geo_opportunity_status") or "NEEDS_REVIEW",
                    "outreach_ready_status": "READY" if state == "OUTREACH_READY" else "NOT_READY",
                    "qualification_status": candidate.get("geo_eligibility_status") or "UNKNOWN",
                    "recruitment_status": "BLOCKED" if state == "BLOCKED" else "NOT_INVITED",
                    "questionnaire_status": "NOT_INVITED", "consent_status": "NOT_ASKED",
                    "geo_property_id": None, "geo_research_status": "AWAITING_CONSENT",
                    "monitoring_status": "NOT_STARTED", "created_at": now, "updated_at": now,
                }
                source_checksum = _sha(raw)
                if existing and existing["source_row_checksum"] == source_checksum:
                    pass
                elif existing:
                    lifecycle["created_at"] = existing["created_at"]
                    db.execute(
                        """UPDATE research_records SET profile_version=:profile_version,profile_type=:profile_type,name=:name,official_website=:official_website,industry=:industry,location=:location,size_status=:size_status,size_evidence_json=:size_evidence_json,verification_evidence_json=:verification_evidence_json,provenance_json=:provenance_json,contact_channels_json=:contact_channels_json,cohort_membership_json=:cohort_membership_json,identity_verification_status=:identity_verification_status,geographic_classification=:geographic_classification,geographic_evidence_json=:geographic_evidence_json,contact_verification_status=CASE WHEN contact_verification_status='CONTACT_VERIFIED' THEN contact_verification_status ELSE :contact_verification_status END,qualification_status=:qualification_status,updated_at=:updated_at WHERE profile_id=:profile_id""", lifecycle
                    )
                else:
                    db.execute(
                        """INSERT INTO research_records (profile_id,profile_version,profile_type,name,official_website,industry,location,size_status,size_evidence_json,verification_evidence_json,provenance_json,contact_channels_json,cohort_membership_json,identity_verification_status,geographic_classification,geographic_evidence_json,contact_verification_status,screening_status,recruitment_priority,outreach_ready_status,qualification_status,recruitment_status,questionnaire_status,consent_status,geo_property_id,geo_research_status,monitoring_status,created_at,updated_at) VALUES (:profile_id,:profile_version,:profile_type,:name,:official_website,:industry,:location,:size_status,:size_evidence_json,:verification_evidence_json,:provenance_json,:contact_channels_json,:cohort_membership_json,:identity_verification_status,:geographic_classification,:geographic_evidence_json,:contact_verification_status,:screening_status,:recruitment_priority,:outreach_ready_status,:qualification_status,:recruitment_status,:questionnaire_status,:consent_status,:geo_property_id,:geo_research_status,:monitoring_status,:created_at,:updated_at)""", lifecycle
                    )
                if not (existing and existing["source_row_checksum"] == source_checksum):
                    db.execute(
                        """UPDATE research_records SET promotion_state=CASE WHEN promotion_state IN ('INVITED','RESEARCH_PARTICIPANT') THEN promotion_state ELSE ? END,
                           prior_contact_status=?, suppression_status=?, source_candidate_id=?, source_row_checksum=? WHERE profile_id=?""",
                        (state, prior_contact, "SUPPRESSED" if has_suppressed_contact else "NOT_SUPPRESSED", candidate_id, source_checksum, prospect_id),
                    )
                _upsert(db, "prospect_provenance", {
                    "provenance_id": _sha(f"{prospect_id}|{candidate.get('source_name')}|{candidate.get('source_record_id')}"),
                    "prospect_id": prospect_id, "source_name": candidate.get("source_name"), "source_record_id": candidate.get("source_record_id"),
                    "source_url": candidate.get("source_url"), "source_license_or_permission": candidate.get("source_license_or_permission"),
                    "discovery_timestamp": candidate.get("discovery_timestamp"), "source_row_checksum": _sha(raw),
                }, "provenance_id")
                for item in channels:
                    contact_raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
                    email = str(item.get("email") or "").strip() or None
                    url = str(item.get("channel_url") or "").strip() or None
                    contact_id = _sha(f"{prospect_id}|{item.get('channel_type')}|{email or ''}|{url or ''}")
                    _upsert(db, "prospect_contacts", {
                        "contact_id": contact_id, "prospect_id": prospect_id, "channel_type": item.get("channel_type") or "UNKNOWN",
                        "email": email, "channel_url": url, "source_url": item.get("source_url"), "evidence": item.get("evidence"),
                        "purpose": item.get("purpose"), "contact_review_status": "CONTACT_REVIEW_PENDING",
                        "email_verification_status": "NOT_VERIFIED", "captured_at": item.get("discovered_at") or candidate.get("last_inspected_at"),
                        "source_row_checksum": _sha(contact_raw),
                    }, "contact_id")
                    counts["contacts"] += 1
                _upsert(db, "prospect_lifecycle_events", {
                    "event_id": _sha(f"{prospect_id}|PROSPECT"), "prospect_id": prospect_id, "event_type": "PROSPECT",
                    "event_value": "CONTACT_REVIEW_PENDING", "event_at": now,
                }, "event_id")
            report["hotels" if hotel else "companies"] = counts
        finally:
            source.close()

    process(business_db, hotel=False)
    process(hotel_db, hotel=True)
    db.commit()
    report["totals"] = {
        "prospects": int(db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type IN ('business_company','hotel_property')").fetchone()[0]),
        "contacts": int(db.execute("SELECT COUNT(*) FROM prospect_contacts").fetchone()[0]),
        "outreach_ready": int(db.execute("SELECT COUNT(*) FROM research_records WHERE outreach_ready_status='READY'").fetchone()[0]),
        "blocked": int(db.execute("SELECT COUNT(*) FROM research_records WHERE recruitment_status='BLOCKED'").fetchone()[0]),
    }
    db.close()
    return report


def review_contact(*, prospect_id: str, contact_id: str, decision: str, reviewer: str, path: str | Path) -> dict[str, Any]:
    """Apply the human contact decision to the canonical lifecycle record."""
    decision = decision.upper()
    if decision not in {"APPROVED", "REJECTED"} or not reviewer.strip():
        raise ValueError("decision must be APPROVED or REJECTED and reviewer is required")
    init_lean_registry(path)
    db = connect_database(path)
    if isinstance(db, sqlite3.Connection):
        db.row_factory = sqlite3.Row
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        contact = db.execute("SELECT prospect_id FROM prospect_contacts WHERE contact_id=?", (contact_id,)).fetchone()
        if not contact or contact["prospect_id"] != prospect_id:
            raise KeyError(contact_id)
        status = "CONTACT_VERIFIED" if decision == "APPROVED" else "CONTACT_REJECTED"
        db.execute("UPDATE prospect_contacts SET contact_review_status=? WHERE contact_id=?", (status, contact_id))
        approved = db.execute("SELECT COUNT(*) FROM prospect_contacts WHERE prospect_id=? AND contact_review_status='CONTACT_VERIFIED'", (prospect_id,)).fetchone()[0]
        if approved:
            db.execute("UPDATE research_records SET contact_verification_status='CONTACT_VERIFIED',outreach_ready_status='READY',promotion_state='OUTREACH_READY',updated_at=? WHERE profile_id=?", (timestamp, prospect_id))
        elif decision == "REJECTED":
            db.execute("UPDATE research_records SET contact_verification_status='CONTACT_REJECTED',outreach_ready_status='NOT_READY',updated_at=? WHERE profile_id=?", (timestamp, prospect_id))
        event = _sha(f"{prospect_id}|CONTACT_REVIEW|{contact_id}|{decision}")
        _upsert(db, "prospect_lifecycle_events", {"event_id": event, "prospect_id": prospect_id, "event_type": "CONTACT_REVIEW", "event_value": f"{decision}:{reviewer}", "event_at": timestamp}, "event_id")
        db.commit()
        return {"prospect_id": prospect_id, "contact_id": contact_id, "decision": decision, "outreach_ready": bool(approved)}
    finally:
        db.close()
