"""Outreach-first research lifecycle with a separate local registry.

The registry is a read model/lifecycle store. Existing Phase 6/7 databases are
read-only inputs and are never migrated or overwritten here. All email, form,
GEO Engine, and monitoring operations are represented as reviewed records; no
network side effect is performed by this module.
"""

from __future__ import annotations

import csv
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
from contextlib import nullcontext
from email.utils import parseaddr
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.integration import CONTRACT_VERSION, PROFILE_VERSION, OWNER_CONTROLLED_TEST
from outreach_agent.database import connect_database, is_postgres_target
from outreach_agent.geographic_eligibility import classify_geography, is_us_eligible
from outreach_agent.google_forms_integration import make_invitation_reference, prefilled_form_url
from outreach_agent.google_forms_integration import parse_sheet_row

REGISTRY_DB: Path | str = os.environ.get("OUTREACH_DATABASE_URL", Path("data/research_registry.db"))
CONSENTED = "CONSENTED"
CONSENT_VERSION = "geo-research-consent/v1"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONFIRMATION_POLICIES = {"AFFIRMATIVE_REPLY_REQUIRED", "NOTICE_ONLY"}


def configured_confirmation_policy() -> str:
    """Return the explicitly configured post-form confirmation policy.

    The safe default requires an affirmative response.  A notice-only flow is
    available only when an operator configures it deliberately; it is never
    inferred from a form submission or from the absence of a reply.
    """
    value = os.environ.get("OUTREACH_CONFIRMATION_POLICY", "AFFIRMATIVE_REPLY_REQUIRED").strip().upper()
    if value not in CONFIRMATION_POLICIES:
        raise ValueError(
            "OUTREACH_CONFIRMATION_POLICY must be AFFIRMATIVE_REPLY_REQUIRED or NOTICE_ONLY"
        )
    return value

# The outreach-first lifecycle is intentionally broader than the GEO Engine's
# internal execution lifecycle.  The owner-controlled fixture uses this same
# ordered contract, with external actions represented as simulated records.
OUTREACH_FIRST_LIFECYCLE = (
    "DISCOVERED", "CONTACT_VERIFIED", "PRELIMINARY_GEO_SCREENED",
    "EMAIL_DRAFTED", "INVITED (SIMULATED)", "QUESTIONNAIRE_SUBMITTED (SIMULATED)",
    "CONSENT_VERIFIED", "GEO_HANDOFF", "GEO_PROPERTY_RESOLVED", "WEBSITE_AUDIT",
    "QUESTION_SET_CREATED", "BASELINE_READY", "STRATEGY_RECOMMENDATIONS_READY",
    "REPORT_READY", "IMPLEMENTATION_PENDING", "MONITORING_PENDING",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def questionnaire_url(token: str, *, base_url: str | None = None) -> str:
    """Build a questionnaire link without falling back to localhost in production."""
    configured = (base_url or os.environ.get("RESEARCH_QUESTIONNAIRE_BASE_URL") or "").rstrip("/")
    if not configured:
        if os.environ.get("OUTREACH_ENV", "local").casefold() == "local":
            configured = "http://127.0.0.1:8765"
        else:
            raise RuntimeError("RESEARCH_QUESTIONNAIRE_BASE_URL is required outside local development")
    return f"{configured}/questionnaire?token={token}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _connect(path: Path | str):
    db = connect_database(path)
    if not is_postgres_target(path):
        db.row_factory = sqlite3.Row
    return db


def init_registry(path: Path | str = REGISTRY_DB) -> Path | str:
    if not is_postgres_target(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS research_records (
          profile_id TEXT PRIMARY KEY, profile_version TEXT NOT NULL, profile_type TEXT NOT NULL,
          name TEXT NOT NULL, official_website TEXT NOT NULL, industry TEXT, location TEXT,
          size_status TEXT, size_evidence_json TEXT NOT NULL DEFAULT '[]', verification_evidence_json TEXT NOT NULL DEFAULT '[]',
          provenance_json TEXT NOT NULL DEFAULT '[]', contact_channels_json TEXT NOT NULL DEFAULT '[]',
          cohort_membership_json TEXT NOT NULL DEFAULT '{}', identity_verification_status TEXT NOT NULL DEFAULT 'UNKNOWN',
          geographic_classification TEXT NOT NULL DEFAULT 'COUNTRY_UNKNOWN', geographic_evidence_json TEXT NOT NULL DEFAULT '{}',
          contact_verification_status TEXT NOT NULL DEFAULT 'NOT_VERIFIED', screening_status TEXT NOT NULL DEFAULT 'NOT_SCREENED',
          recruitment_priority TEXT NOT NULL DEFAULT 'NEEDS_REVIEW', outreach_ready_status TEXT NOT NULL DEFAULT 'NOT_READY',
          qualification_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW', recruitment_status TEXT NOT NULL DEFAULT 'NOT_INVITED',
          questionnaire_status TEXT NOT NULL DEFAULT 'NOT_INVITED', consent_status TEXT NOT NULL DEFAULT 'NOT_ASKED',
          geo_property_id INTEGER, geo_research_status TEXT NOT NULL DEFAULT 'NOT_ELIGIBLE',
          monitoring_status TEXT NOT NULL DEFAULT 'NOT_STARTED', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS screenings (
          screening_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL,
          status TEXT NOT NULL, opportunities_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
          provider TEXT, model TEXT, prompt_version TEXT, methodology TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY(profile_id) REFERENCES research_records(profile_id)
        );
        CREATE TABLE IF NOT EXISTS screening_analysis_results (
          analysis_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL,
          mode TEXT NOT NULL, content_hash TEXT NOT NULL, result_json TEXT NOT NULL,
          input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
          estimated_cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
          UNIQUE(profile_id, content_hash, mode)
        );
        CREATE TABLE IF NOT EXISTS questionnaire_invitations (
          invitation_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, invitation_reference TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
          token_expires_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questionnaire_submissions (
          submission_id TEXT PRIMARY KEY, invitation_id TEXT NOT NULL, response_hash TEXT NOT NULL,
          respondent_name TEXT, respondent_role TEXT, contact_email TEXT, consent_status TEXT NOT NULL,
          company_identity_confirmed INTEGER NOT NULL DEFAULT 0,
          website_analysis_allowed INTEGER NOT NULL, recommendations_requested INTEGER NOT NULL,
          ongoing_measurement_allowed INTEGER NOT NULL, site_modification_authorized INTEGER NOT NULL,
          future_contact_permission INTEGER NOT NULL DEFAULT 0,
          consent_wording_version TEXT NOT NULL, submitted_at TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'PENDING',
          response_source TEXT NOT NULL DEFAULT 'LOCAL_TOKEN', invitation_reference TEXT,
          invitation_recipient_email TEXT, representative_email TEXT,
          UNIQUE(invitation_id, response_hash)
        );
        CREATE TABLE IF NOT EXISTS email_drafts (
          draft_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, draft_version TEXT NOT NULL,
          template_version TEXT NOT NULL, recipient TEXT NOT NULL, preview_hash TEXT NOT NULL,
          body TEXT NOT NULL, approval_status TEXT NOT NULL DEFAULT 'PENDING', approved_by TEXT, approved_at TEXT,
          subject TEXT NOT NULL DEFAULT '', analysis_id TEXT, invitation_id TEXT
        );
        CREATE TABLE IF NOT EXISTS confirmation_emails (
          confirmation_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, invitation_id TEXT NOT NULL,
          submission_id TEXT NOT NULL, draft_id TEXT NOT NULL UNIQUE,
          official_company_email TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT',
          confirmation_policy TEXT NOT NULL DEFAULT 'AFFIRMATIVE_REPLY_REQUIRED',
          provider_message_id TEXT, created_at TEXT NOT NULL, sent_at TEXT,
          confirmed_at TEXT, confirmation_evidence_ref TEXT, review_status TEXT NOT NULL DEFAULT 'PENDING',
          UNIQUE(profile_id, submission_id)
        );
        CREATE TABLE IF NOT EXISTS research_reports (
          report_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, source_profile_version TEXT NOT NULL,
          property_id INTEGER NOT NULL, result_record_id TEXT NOT NULL UNIQUE,
          report_path TEXT, status TEXT NOT NULL DEFAULT 'PREVIEW_ONLY',
          delivery_status TEXT NOT NULL DEFAULT 'NOT_SENT',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(profile_id, source_profile_version)
        );
        CREATE TABLE IF NOT EXISTS email_events (
          event_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, event_type TEXT NOT NULL,
          provider TEXT NOT NULL, provider_enabled INTEGER NOT NULL, message_id TEXT, error TEXT, created_at TEXT NOT NULL,
          idempotency_key TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS dispatch_attempts (
          attempt_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
          provider TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
          attempt_number INTEGER NOT NULL, status TEXT NOT NULL, provider_message_id TEXT,
          error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(draft_id, idempotency_key, attempt_number)
        );
        CREATE TABLE IF NOT EXISTS suppressed_recipients (recipient TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS replies (
          reply_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, thread_id TEXT NOT NULL,
          sender TEXT NOT NULL, classification TEXT NOT NULL, classification_source TEXT NOT NULL,
          review_status TEXT NOT NULL DEFAULT 'PENDING', original_reference TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS geo_handoffs (
          handoff_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, profile_version TEXT NOT NULL,
          contract_version TEXT NOT NULL, consent_submission_id TEXT NOT NULL, scope_json TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(profile_id, profile_version)
        );
        CREATE TABLE IF NOT EXISTS consent_reviews (
          review_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL, profile_id TEXT NOT NULL,
          decision TEXT NOT NULL, reviewer TEXT NOT NULL,
          company_identity_verified INTEGER NOT NULL, representative_authorized INTEGER NOT NULL,
          consent_version_verified INTEGER NOT NULL, website_scope_verified INTEGER NOT NULL,
          withdrawal_checked INTEGER NOT NULL, notes TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questionnaire_conflicts (
          conflict_id TEXT PRIMARY KEY, invitation_id TEXT NOT NULL, profile_id TEXT NOT NULL,
          first_submission_id TEXT NOT NULL, conflicting_submission_id TEXT NOT NULL,
          reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'QUARANTINED',
          created_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT
        );
        CREATE TABLE IF NOT EXISTS geo_handoff_attempts (
          attempt_id TEXT PRIMARY KEY, handoff_id TEXT NOT NULL, profile_id TEXT NOT NULL,
          submission_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
          property_id INTEGER, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS research_filter_idx ON research_records(profile_type, recruitment_status, consent_status, geo_research_status);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(questionnaire_submissions)")}
        submission_additions = {
            "research_interest": "TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'",
            "research_authorization": "TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'",
            "representative_full_name": "TEXT",
            "representative_job_title": "TEXT",
            "identity_review_status": "TEXT NOT NULL DEFAULT 'PENDING'",
            "invitation_recipient_email": "TEXT",
            "representative_email": "TEXT",
        }
        for name, definition in submission_additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE questionnaire_submissions ADD COLUMN {name} {definition}")
        if "company_identity_confirmed" not in columns:
            db.execute("ALTER TABLE questionnaire_submissions ADD COLUMN company_identity_confirmed INTEGER NOT NULL DEFAULT 0")
        if "future_contact_permission" not in columns:
            db.execute("ALTER TABLE questionnaire_submissions ADD COLUMN future_contact_permission INTEGER NOT NULL DEFAULT 0")
        if "response_source" not in columns:
            db.execute("ALTER TABLE questionnaire_submissions ADD COLUMN response_source TEXT NOT NULL DEFAULT 'LOCAL_TOKEN'")
        if "invitation_reference" not in columns:
            db.execute("ALTER TABLE questionnaire_submissions ADD COLUMN invitation_reference TEXT")
        invitation_columns = {row[1] for row in db.execute("PRAGMA table_info(questionnaire_invitations)")}
        if "invitation_reference" not in invitation_columns:
            db.execute("ALTER TABLE questionnaire_invitations ADD COLUMN invitation_reference TEXT")
            rows = db.execute("SELECT invitation_id FROM questionnaire_invitations WHERE invitation_reference IS NULL").fetchall()
            for item in rows:
                reference = make_invitation_reference(str(item["invitation_id"]), nonce=secrets.token_urlsafe(8))
                db.execute("UPDATE questionnaire_invitations SET invitation_reference=? WHERE invitation_id=?", (reference, item["invitation_id"]))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS questionnaire_invitation_reference_idx ON questionnaire_invitations(invitation_reference)")
        record_columns = {row[1] for row in db.execute("PRAGMA table_info(research_records)")}
        additions = {
            "cohort_membership_json": "TEXT NOT NULL DEFAULT '{}'",
            "identity_verification_status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "contact_verification_status": "TEXT NOT NULL DEFAULT 'NOT_VERIFIED'",
            "screening_status": "TEXT NOT NULL DEFAULT 'NOT_SCREENED'",
            "recruitment_priority": "TEXT NOT NULL DEFAULT 'NEEDS_REVIEW'",
            "outreach_ready_status": "TEXT NOT NULL DEFAULT 'NOT_READY'",
            "geographic_classification": "TEXT NOT NULL DEFAULT 'COUNTRY_UNKNOWN'",
            "geographic_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "readiness_reason_json": "TEXT NOT NULL DEFAULT '[]'",
            "screening_provider": "TEXT",
            "screening_model": "TEXT",
            "screening_prompt_version": "TEXT",
            "screening_completed_at": "TEXT",
            "promotion_state": "TEXT NOT NULL DEFAULT 'DISCOVERY'",
            "prior_contact_status": "TEXT NOT NULL DEFAULT 'NONE'",
            "suppression_status": "TEXT NOT NULL DEFAULT 'NOT_SUPPRESSED'",
            "withdrawal_status": "TEXT NOT NULL DEFAULT 'NOT_WITHDRAWN'",
            "source_candidate_id": "TEXT",
            "source_row_checksum": "TEXT",
        }
        for name, definition in additions.items():
            if name not in record_columns:
                db.execute(f"ALTER TABLE research_records ADD COLUMN {name} {definition}")
        email_columns = {row[1] for row in db.execute("PRAGMA table_info(email_events)")}
        if "idempotency_key" not in email_columns:
            db.execute("ALTER TABLE email_events ADD COLUMN idempotency_key TEXT")
        draft_columns = {row[1] for row in db.execute("PRAGMA table_info(email_drafts)")}
        for name, definition in {
            "subject": "TEXT NOT NULL DEFAULT ''", "analysis_id": "TEXT", "invitation_id": "TEXT",
            # Keep the intended company contact distinct from a deliberately
            # selected owner-controlled test delivery address.
            "intended_recipient": "TEXT",
            "delivery_recipient": "TEXT",
            "recipient_mode": "TEXT NOT NULL DEFAULT 'OFFICIAL_CONTACT'",
            "test_override": "INTEGER NOT NULL DEFAULT 0",
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if name not in draft_columns:
                db.execute(f"ALTER TABLE email_drafts ADD COLUMN {name} {definition}")
        reply_columns = {row[1] for row in db.execute("PRAGMA table_info(replies)")}
        for name, definition in {
            "recipient": "TEXT NOT NULL DEFAULT ''",
            "subject": "TEXT NOT NULL DEFAULT ''",
            "message_id": "TEXT",
            "in_reply_to": "TEXT",
            "references_header": "TEXT",
            "sent_at": "TEXT",
            "raw_content": "TEXT NOT NULL DEFAULT ''",
            "invitation_reference": "TEXT",
            "correlation_status": "TEXT NOT NULL DEFAULT 'MANUAL_REVIEW'",
        }.items():
            if name not in reply_columns:
                db.execute(f"ALTER TABLE replies ADD COLUMN {name} {definition}")
        confirmation_columns = {row[1] for row in db.execute("PRAGMA table_info(confirmation_emails)")}
        for name, definition in {
            "confirmation_policy": "TEXT NOT NULL DEFAULT 'AFFIRMATIVE_REPLY_REQUIRED'",
            "confirmed_at": "TEXT",
            "confirmation_evidence_ref": "TEXT",
            "review_status": "TEXT NOT NULL DEFAULT 'PENDING'",
        }.items():
            if name not in confirmation_columns:
                db.execute(f"ALTER TABLE confirmation_emails ADD COLUMN {name} {definition}")
        record_columns = {row[1] for row in db.execute("PRAGMA table_info(research_records)")}
        if "geo_integration_status" not in record_columns:
            db.execute("ALTER TABLE research_records ADD COLUMN geo_integration_status TEXT NOT NULL DEFAULT 'NOT_STARTED'")
        if "geo_integration_error" not in record_columns:
            db.execute("ALTER TABLE research_records ADD COLUMN geo_integration_error TEXT")
        handoff_columns = {row[1] for row in db.execute("PRAGMA table_info(geo_handoffs)")}
        for name, definition in {"property_id": "INTEGER", "attempt_count": "INTEGER NOT NULL DEFAULT 0", "last_error": "TEXT", "updated_at": "TEXT"}.items():
            if name not in handoff_columns:
                db.execute(f"ALTER TABLE geo_handoffs ADD COLUMN {name} {definition}")
    return path


def register_profile(profile: dict[str, Any], path: Path = REGISTRY_DB) -> dict[str, Any]:
    """Register a profile snapshot without changing its source database."""
    if profile.get("schema_version") != PROFILE_VERSION:
        raise ValueError("only geo-company-profile/v1 profiles may be registered")
    identity = profile.get("identity") or {}
    profile_id = str(profile.get("profile_id") or "")
    if not profile_id or not identity.get("website"):
        raise ValueError("profile_id and official website are required")
    ptype = {"hotel_property": "hotel_property", "startup_company": "business_company", "owner_controlled_test": "owner_controlled_test"}.get(profile.get("profile_type"), "business_company")
    if ptype == "owner_controlled_test":
        recruitment, consent, geo = "EXCLUDED", "NOT_APPLICABLE", "NOT_ELIGIBLE"
    else:
        recruitment, consent, geo = "NOT_INVITED", "NOT_ASKED", "AWAITING_CONSENT"
    timestamp = now()
    location = (profile.get("hotel") or {}).get("location") or (profile.get("startup") or {}).get("location") or ""
    geographic_classification, geographic_evidence = classify_geography(location=location, source_name=(profile.get("provenance") or {}).get("source_name"), source_batch=(profile.get("provenance") or {}).get("source_batch"), profile_type=ptype)
    row = {"profile_id": profile_id, "profile_version": PROFILE_VERSION, "profile_type": ptype, "name": identity.get("name", profile_id), "official_website": identity["website"], "industry": (profile.get("hotel") or {}).get("category") or (profile.get("startup") or {}).get("industry"), "location": location, "size_status": (profile.get("startup") or {}).get("size_evidence_status", "SIZE_UNKNOWN"), "size_evidence_json": json.dumps([], sort_keys=True), "verification_evidence_json": json.dumps(identity.get("verification_evidence", []), sort_keys=True), "provenance_json": json.dumps(profile.get("provenance", {}), sort_keys=True), "contact_channels_json": json.dumps(profile.get("contact_channels", []), sort_keys=True), "cohort_membership_json": json.dumps(profile.get("cohort_membership", {}), sort_keys=True), "identity_verification_status": identity.get("verification_status", "UNKNOWN"), "geographic_classification": geographic_classification, "geographic_evidence_json": json.dumps(geographic_evidence, sort_keys=True), "contact_verification_status": "NOT_VERIFIED", "screening_status": "NOT_SCREENED", "recruitment_priority": "NEEDS_REVIEW", "outreach_ready_status": "NOT_READY", "qualification_status": ((profile.get("qualification") or {}).get("eligibility") or "NEEDS_REVIEW"), "recruitment_status": recruitment, "questionnaire_status": "NOT_INVITED", "consent_status": consent, "geo_property_id": None, "geo_research_status": geo, "monitoring_status": "NOT_STARTED", "created_at": timestamp, "updated_at": timestamp}
    init_registry(path)
    with _connect(path) as db:
        existing = db.execute("SELECT created_at FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        if existing:
            row["created_at"] = existing["created_at"]
            db.execute("""UPDATE research_records SET profile_version=:profile_version,profile_type=:profile_type,name=:name,official_website=:official_website,industry=:industry,location=:location,size_status=:size_status,size_evidence_json=:size_evidence_json,verification_evidence_json=:verification_evidence_json,provenance_json=:provenance_json,contact_channels_json=:contact_channels_json,cohort_membership_json=:cohort_membership_json,identity_verification_status=:identity_verification_status,geographic_classification=:geographic_classification,geographic_evidence_json=:geographic_evidence_json,qualification_status=:qualification_status,updated_at=:updated_at WHERE profile_id=:profile_id""", row)
        else:
            db.execute("""INSERT INTO research_records
              (profile_id,profile_version,profile_type,name,official_website,industry,location,size_status,size_evidence_json,verification_evidence_json,provenance_json,contact_channels_json,cohort_membership_json,identity_verification_status,geographic_classification,geographic_evidence_json,contact_verification_status,screening_status,recruitment_priority,outreach_ready_status,qualification_status,recruitment_status,questionnaire_status,consent_status,geo_property_id,geo_research_status,monitoring_status,created_at,updated_at)
              VALUES (:profile_id,:profile_version,:profile_type,:name,:official_website,:industry,:location,:size_status,:size_evidence_json,:verification_evidence_json,:provenance_json,:contact_channels_json,:cohort_membership_json,:identity_verification_status,:geographic_classification,:geographic_evidence_json,:contact_verification_status,:screening_status,:recruitment_priority,:outreach_ready_status,:qualification_status,:recruitment_status,:questionnaire_status,:consent_status,:geo_property_id,:geo_research_status,:monitoring_status,:created_at,:updated_at)""", row)
    return row


def save_preliminary_screening(profile_id: str, opportunities: list[dict[str, Any]], evidence: list[dict[str, Any]], *, path: Path = REGISTRY_DB, provider: str | None = None, model: str | None = None, prompt_version: str | None = None, methodology: str | None = None) -> str:
    if not 1 <= len(opportunities) <= 3:
        raise ValueError("preliminary screening must contain 1-3 opportunities")
    if any(any(token in json.dumps(item).lower() for token in ("% uplift", "% increase", "precise percentage")) for item in opportunities):
        raise ValueError("unvalidated screening cannot contain precise uplift claims")
    screening_id = "screening-" + secrets.token_urlsafe(10)
    init_registry(path)
    with _connect(path) as db:
        record = db.execute("SELECT profile_type,geographic_classification FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        if not record:
            raise ValueError("profile is not registered")
        if not is_us_eligible(record["geographic_classification"]):
            raise PermissionError("preliminary GEO screening requires VERIFIED_US geographic eligibility")
        db.execute("INSERT INTO screenings VALUES (?,?,?,?,?,?,?,?,?,?,?)", (screening_id, profile_id, PROFILE_VERSION, "PRELIMINARY_UNVALIDATED", json.dumps(opportunities, sort_keys=True), json.dumps(evidence, sort_keys=True), provider, model, prompt_version, methodology, now()))
    return screening_id


def create_questionnaire_invitation(profile_id: str, *, secret: str, path: Path = REGISTRY_DB, expires_at: str | None = None) -> tuple[str, str]:
    init_registry(path)
    raw = secrets.token_urlsafe(32)
    token_hash = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    invitation_id = "invite-" + secrets.token_urlsafe(10)
    invitation_reference = make_invitation_reference(profile_id, nonce=invitation_id)
    with _connect(path) as db:
        record = db.execute("SELECT profile_type,geographic_classification FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        if not record:
            raise ValueError("profile is not registered")
        if record["profile_type"] != "owner_controlled_test" and not is_us_eligible(record["geographic_classification"]):
            raise PermissionError("outreach invitation requires VERIFIED_US geographic eligibility")
        db.execute("INSERT INTO questionnaire_invitations(invitation_id,profile_id,invitation_reference,token_hash,token_expires_at,status,created_at) VALUES (?,?,?,?,?,?,?)", (invitation_id, profile_id, invitation_reference, token_hash, expires_at, "INVITED", now()))
        db.execute("UPDATE research_records SET recruitment_status='INVITED',questionnaire_status='INVITED',promotion_state=CASE WHEN promotion_state='OUTREACH_READY' THEN 'INVITED' ELSE promotion_state END,updated_at=? WHERE profile_id=?", (now(), profile_id))
    return invitation_id, raw


def get_invitation_reference(invitation_id: str, *, path: Path = REGISTRY_DB) -> str:
    init_registry(path)
    with _connect(path) as db:
        row = db.execute("SELECT invitation_reference FROM questionnaire_invitations WHERE invitation_id=?", (invitation_id,)).fetchone()
    if not row:
        raise KeyError(invitation_id)
    return str(row["invitation_reference"])


def submit_questionnaire(token: str, *, secret: str, response: dict[str, Any], path: Path = REGISTRY_DB) -> str:
    token_hash = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    response_hash = _hash(json.dumps(response, sort_keys=True, separators=(",", ":")))
    init_registry(path)
    with _connect(path) as db:
        invitation = db.execute("SELECT * FROM questionnaire_invitations WHERE token_hash=?", (token_hash,)).fetchone()
        if not invitation:
            raise ValueError("invalid questionnaire token")
        if invitation["token_expires_at"]:
            try:
                if datetime.fromisoformat(invitation["token_expires_at"]) <= datetime.now(timezone.utc):
                    db.execute("UPDATE questionnaire_invitations SET status='EXPIRED' WHERE invitation_id=?", (invitation["invitation_id"],))
                    raise ValueError("questionnaire token has expired")
            except ValueError as exc:
                if "expired" in str(exc):
                    raise
                raise ValueError("invalid questionnaire expiry") from exc
        prior = db.execute("SELECT submission_id FROM questionnaire_submissions WHERE invitation_id=? AND response_hash=?", (invitation["invitation_id"], response_hash)).fetchone()
        if prior:
            return prior["submission_id"]
        submission_id = "submission-" + secrets.token_urlsafe(10)
        conflicting = db.execute("SELECT 1 FROM questionnaire_submissions WHERE invitation_id=? LIMIT 1", (invitation["invitation_id"],)).fetchone()
        consent = "NEEDS_REVIEW" if conflicting else ("CONSENTED" if response.get("agree_research") is True and response.get("company_identity_confirmed") is True else ("DECLINED" if response.get("agree_research") is False else "NEEDS_REVIEW"))
        db.execute("""INSERT INTO questionnaire_submissions
          (submission_id,invitation_id,response_hash,respondent_name,respondent_role,contact_email,consent_status,
           company_identity_confirmed,website_analysis_allowed,recommendations_requested,ongoing_measurement_allowed,
           site_modification_authorized,future_contact_permission,consent_wording_version,submitted_at,review_status)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (submission_id, invitation["invitation_id"], response_hash, response.get("respondent_name"), response.get("respondent_role"), response.get("contact_email"), consent, int(bool(response.get("company_identity_confirmed"))), int(bool(response.get("website_analysis_allowed"))), int(bool(response.get("recommendations_requested"))), int(bool(response.get("ongoing_measurement_allowed"))), int(bool(response.get("site_modification_authorized"))), int(bool(response.get("future_contact_permission"))), response.get("consent_wording_version", CONSENT_VERSION), response.get("submitted_at", now()), "NEEDS_REVIEW" if conflicting else "PENDING"))
        db.execute("UPDATE questionnaire_invitations SET status='RESPONDED' WHERE invitation_id=?", (invitation["invitation_id"],))
        db.execute("UPDATE research_records SET questionnaire_status='RESPONDED',consent_status=?,recruitment_status='RESPONDED',updated_at=? WHERE profile_id=?", (consent, now(), invitation["profile_id"]))
    return submission_id


def ingest_google_form_response(row: dict[str, Any], *, path: Path = REGISTRY_DB) -> str:
    """Import one authenticated Sheets row without granting automatic consent."""
    parsed = parse_sheet_row(row)
    init_registry(path)
    response_hash = _hash(json.dumps(parsed.raw, sort_keys=True, separators=(",", ":")))
    with _connect(path) as db:
        invitation = db.execute("SELECT * FROM questionnaire_invitations WHERE invitation_reference=?", (parsed.invitation_reference,)).fetchone()
        if not invitation:
            return "UNKNOWN_REFERENCE"
        duplicate = db.execute("SELECT submission_id FROM questionnaire_submissions WHERE invitation_id=? AND response_hash=?", (invitation["invitation_id"], response_hash)).fetchone()
        if duplicate:
            return "DUPLICATE"
        prior_submission = db.execute("SELECT submission_id FROM questionnaire_submissions WHERE invitation_id=? ORDER BY submitted_at LIMIT 1", (invitation["invitation_id"],)).fetchone()
        allowed_emails = {str(invitation_email[0]).casefold() for invitation_email in db.execute("SELECT recipient FROM email_drafts WHERE invitation_id=?", (invitation["invitation_id"],)).fetchall()}
        invitation_email_matches = parsed.contact_email.casefold() in allowed_emails if parsed.contact_email else False
        interest = parsed.research_interest
        authorization = parsed.research_authorization
        valid_pair = interest in {"YES", "NO"} and authorization in {"YES", "NO"}
        conflicting = prior_submission is not None
        if conflicting or not valid_pair or not parsed.representative_name or not parsed.representative_role or not parsed.contact_email:
            outcome = "NEEDS_REVIEW"
            review = "CONFLICT_QUARANTINED" if conflicting else "NEEDS_REVIEW"
        elif interest == "YES" and authorization == "YES":
            outcome = "NEEDS_REVIEW"
            review = "NEEDS_REVIEW"
        elif interest == "YES" and authorization == "NO":
            outcome = "INTERESTED_NOT_AUTHORIZED"
            review = "NEEDS_REVIEW" if not invitation_email_matches else "PENDING"
        elif interest == "NO" and authorization == "NO":
            outcome = "DECLINED"
            review = "NEEDS_REVIEW" if not invitation_email_matches else "PENDING"
        else:
            outcome = "NEEDS_REVIEW"
            review = "NEEDS_REVIEW"
        identity_review = "MATCHED_INVITATION_EMAIL" if invitation_email_matches else "REPRESENTATIVE_EMAIL_DIFFERS_FROM_INVITATION"
        invitation_recipient_email = next(iter(sorted(allowed_emails)), "")
        submission_id = "submission-" + _hash(invitation["invitation_id"] + response_hash)[:20]
        db.execute("""INSERT INTO questionnaire_submissions
          (submission_id,invitation_id,response_hash,respondent_name,respondent_role,contact_email,consent_status,
           company_identity_confirmed,website_analysis_allowed,recommendations_requested,ongoing_measurement_allowed,
           site_modification_authorized,future_contact_permission,consent_wording_version,submitted_at,review_status,response_source,invitation_reference,
           research_interest,research_authorization,representative_full_name,representative_job_title,identity_review_status,
           invitation_recipient_email,representative_email)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (submission_id, invitation["invitation_id"], response_hash, parsed.representative_name, parsed.representative_role, parsed.contact_email, outcome, 0, 0, 0, 0, 0, 0, CONSENT_VERSION, parsed.submitted_at or now(), review, "GOOGLE_SHEETS", parsed.invitation_reference, interest, authorization, parsed.representative_name, parsed.representative_role, identity_review, invitation_recipient_email, parsed.contact_email))
        if conflicting:
            conflict_id = "conflict-" + _hash(invitation["invitation_id"] + response_hash)[:20]
            db.execute("""INSERT INTO questionnaire_conflicts
              (conflict_id,invitation_id,profile_id,first_submission_id,conflicting_submission_id,reason,status,created_at)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (conflict_id) DO NOTHING""", (conflict_id, invitation["invitation_id"], invitation["profile_id"], prior_submission["submission_id"], submission_id, "multiple distinct responses for one invitation", "QUARANTINED", now()))
        db.execute("UPDATE questionnaire_invitations SET status='RESPONDED' WHERE invitation_id=?", (invitation["invitation_id"],))
        # PostgreSQL requires a boolean for CASE WHEN; SQLite accepts either
        # representation, so keep the semantic value intact across backends.
        db.execute("UPDATE research_records SET questionnaire_status='RESPONDED',consent_status=?,recruitment_status=?,geo_research_status='PENDING_REVIEW',geo_integration_status=CASE WHEN ? THEN 'BLOCKED_CONFLICT' ELSE geo_integration_status END,updated_at=? WHERE profile_id=?", (outcome, "DECLINED" if outcome == "DECLINED" else "RESPONDED", bool(conflicting), now(), invitation["profile_id"]))
    return outcome if outcome in {"DECLINED", "INTERESTED_NOT_AUTHORIZED"} else "NEEDS_REVIEW"


def withdraw_consent(profile_id: str, *, reason: str | None = None, path: Path = REGISTRY_DB) -> None:
    init_registry(path)
    with _connect(path) as db:
        changed = db.execute("UPDATE research_records SET consent_status='WITHDRAWN',withdrawal_status='WITHDRAWN',promotion_state='BLOCKED',geo_research_status='BLOCKED',monitoring_status='BLOCKED',updated_at=? WHERE profile_id=?", (now(), profile_id)).rowcount
        if not changed:
            raise ValueError("profile is not registered")
        db.execute("UPDATE geo_handoffs SET status='BLOCKED_WITHDRAWN' WHERE profile_id=? AND status<>'COMPLETED'", (profile_id,))


def get_record(profile_id: str, path: Path = REGISTRY_DB) -> dict[str, Any]:
    init_registry(path)
    with _connect(path) as db:
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        if not record:
            raise KeyError(profile_id)
        result = dict(record)
        result["screenings"] = [dict(row) for row in db.execute("SELECT * FROM screenings WHERE profile_id=? ORDER BY created_at DESC", (profile_id,))]
        result["analysis_results"] = [dict(row) for row in db.execute("SELECT * FROM screening_analysis_results WHERE profile_id=? ORDER BY created_at DESC", (profile_id,))]
        result["email_drafts"] = [dict(row) for row in db.execute("SELECT * FROM email_drafts WHERE profile_id=? ORDER BY draft_id DESC", (profile_id,))]
        result["handoffs"] = [dict(row) for row in db.execute("SELECT * FROM geo_handoffs WHERE profile_id=? ORDER BY created_at DESC", (profile_id,))]
        result["questionnaire_submissions"] = [dict(row) for row in db.execute("SELECT * FROM questionnaire_submissions WHERE invitation_id IN (SELECT invitation_id FROM questionnaire_invitations WHERE profile_id=?) ORDER BY submitted_at DESC", (profile_id,))]
        result["questionnaire_conflicts"] = [dict(row) for row in db.execute("SELECT * FROM questionnaire_conflicts WHERE profile_id=? ORDER BY created_at DESC", (profile_id,))]
        result["consent_reviews"] = [dict(row) for row in db.execute("SELECT * FROM consent_reviews WHERE profile_id=? ORDER BY created_at DESC", (profile_id,))]
        result["contact_channels"] = json.loads(result["contact_channels_json"] or "[]")
        return result


def personalized_recruitment_draft(profile_id: str, *, recipient: str, questionnaire_url: str, path: Path = REGISTRY_DB, template_version: str = "research-invitation-v2") -> str:
    record = get_record(profile_id, path)
    if record["profile_type"] == "owner_controlled_test":
        raise PermissionError("owner-controlled test fixture cannot receive normal recruitment")
    screenings = record.get("screenings", [])
    observations = []
    if screenings:
        decoded = json.loads(screenings[0]["opportunities_json"])
        observations = (decoded.get("opportunities") or [])[:2] if isinstance(decoded, dict) else decoded[:2]
    observation_text = "; ".join(item.get("title", "preliminary opportunity") for item in observations) or "a preliminary website review"
    body = (f"Hello,\n\nWe are inviting {record['name']} to participate in a research study on how public website information is represented in AI search. "
            f"Our preliminary, unvalidated review noted: {observation_text}. These are research hypotheses, not measured improvements.\n\n"
            f"Participation details and the company-specific questionnaire are here: {questionnaire_url}\n\n"
            "No website changes or experiments are performed without separate approval.\n")
    return create_email_draft(profile_id, recipient, body, path=path, template_version=template_version)


def approve_email_batch(draft_ids: list[str], reviewer: str, *, path: Path = REGISTRY_DB) -> dict[str, int]:
    approved = 0
    for draft_id in draft_ids:
        approve_email(draft_id, reviewer, path=path)
        approved += 1
    return {"approved": approved}


def record_simulated_send_batch(draft_ids: list[str], *, preview_hashes: dict[str, str] | None = None, path: Path = REGISTRY_DB, max_per_batch: int = 50) -> dict[str, Any]:
    if len(draft_ids) > max_per_batch:
        raise ValueError("batch exceeds configured rate limit")
    events = [record_simulated_send(draft_id, expected_preview_hash=(preview_hashes or {}).get(draft_id), path=path) for draft_id in draft_ids]
    return {"events": events, "provider_enabled": False, "sent": 0, "simulated": len(events)}


def ingest_reply(*, profile_id: str, thread_id: str, message_id: str, sender: str, text: str, path: Path = REGISTRY_DB, verified_sender: bool = False) -> str:
    init_registry(path)
    address = parseaddr(sender)[1].casefold()
    if not verified_sender or not address:
        review = "NEEDS_REVIEW"
    else:
        review = "PENDING"
    reply_id = "reply-" + _hash(message_id)[:16]
    classification = classify_reply(text)
    with _connect(path) as db:
        db.execute("""INSERT INTO replies(reply_id,profile_id,thread_id,sender,classification,classification_source,review_status,original_reference,created_at)
          VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (reply_id) DO NOTHING""", (reply_id, profile_id, thread_id, sender, classification, "RULE_ADVISORY", review, message_id, now()))
        if classification == "UNSUBSCRIBE":
            db.execute("INSERT INTO suppressed_recipients VALUES (?,?,?) ON CONFLICT (recipient) DO UPDATE SET reason=EXCLUDED.reason,created_at=EXCLUDED.created_at", (address, "reply_unsubscribe", now()))
    return reply_id


def ingest_eml_reply(eml_path: Path, *, path: Path = REGISTRY_DB) -> dict[str, Any]:
    """Import one exported Outlook message as review evidence.

    The file is parsed locally and retained verbatim in the registry. Header
    values are evidence, not proof of mailbox authenticity; imported messages
    therefore remain ``NEEDS_REVIEW`` until an operator verifies the sender.
    Correlation uses an invitation reference when present, otherwise a unique
    invitation recipient match. Ambiguous or missing correlation is retained
    with ``AMBIGUOUS``/``UNMATCHED`` status and never changes consent.
    """
    raw_bytes = Path(eml_path).read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    sender = str(message.get("From") or "")
    sender_address = parseaddr(sender)[1].casefold()
    recipients = ", ".join(str(value) for value in message.get_all("To", []) + message.get_all("Cc", []))
    subject = str(message.get("Subject") or "")
    message_id = str(message.get("Message-ID") or "").strip() or "eml-" + _hash(raw_bytes.decode("utf-8", errors="replace"))[:24]
    in_reply_to = str(message.get("In-Reply-To") or "").strip()
    references_header = " ".join(str(value) for value in message.get_all("References", []))
    sent_at = str(message.get("Date") or "").strip()
    if message.is_multipart():
        parts = [part for part in message.walk() if part.get_content_type() == "text/plain" and not part.get_filename()]
        text = "\n\n".join(str(part.get_content()) for part in parts)
    else:
        text = str(message.get_content())
    invitation_references = set(re.findall(r"GEO-[A-Z0-9]{16}", " ".join((subject, text, references_header))))

    init_registry(path)
    with _connect(path) as db:
        candidates: list[tuple[str, str]] = []
        for reference in sorted(invitation_references):
            row = db.execute("SELECT invitation_id,profile_id FROM questionnaire_invitations WHERE invitation_reference=?", (reference,)).fetchone()
            if row:
                candidates.append((row["invitation_id"], row["profile_id"]))
        if not candidates and sender_address:
            rows = db.execute(
                "SELECT DISTINCT i.invitation_id,i.profile_id FROM questionnaire_invitations i JOIN email_drafts d ON d.invitation_id=i.invitation_id WHERE lower(d.recipient)=?",
                (sender_address,),
            ).fetchall()
            candidates = [(row["invitation_id"], row["profile_id"]) for row in rows]
        if len(candidates) == 1:
            invitation_id, profile_id = candidates[0]
            correlation_status = "CORRELATED_BY_REFERENCE" if invitation_references else "CORRELATED_BY_UNIQUE_RECIPIENT"
        elif len(candidates) > 1:
            invitation_id = profile_id = ""
            correlation_status = "AMBIGUOUS"
        else:
            invitation_id = profile_id = ""
            correlation_status = "UNMATCHED"
        reply_id = "reply-" + _hash(message_id)[:16]
        classification = classify_reply(text)
        existing = db.execute("SELECT reply_id FROM replies WHERE reply_id=?", (reply_id,)).fetchone()
        if not existing:
            db.execute(
                """INSERT INTO replies
                (reply_id,profile_id,thread_id,sender,classification,classification_source,review_status,original_reference,created_at,
                 recipient,subject,message_id,in_reply_to,references_header,sent_at,raw_content,invitation_reference,correlation_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (reply_id, profile_id, in_reply_to or message_id, sender, classification, "RULE_ADVISORY", "NEEDS_REVIEW", message_id, now(), recipients, subject, message_id, in_reply_to, references_header, sent_at, raw_bytes.decode("utf-8", errors="replace"), next(iter(invitation_references), ""), correlation_status),
            )
        return {
            "reply_id": reply_id,
            "profile_id": profile_id or None,
            "invitation_id": invitation_id or None,
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references_header,
            "sender": sender,
            "recipient": recipients,
            "subject": subject,
            "sent_at": sent_at,
            "classification": classification,
            "correlation_status": correlation_status,
            "review_status": "NEEDS_REVIEW",
            "consent_changed": False,
        }


def create_geo_handoff(profile_id: str, submission_id: str, *, scope: dict[str, Any], path: Path = REGISTRY_DB) -> str:
    init_registry(path)
    with _connect(path) as db:
        row = db.execute("SELECT * FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        submission = db.execute("SELECT * FROM questionnaire_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if not row or row["consent_status"] != CONSENTED or not submission or submission["consent_status"] != CONSENTED or not submission["website_analysis_allowed"]:
            raise PermissionError("GEO handoff requires explicit website-analysis consent")
        if submission["response_source"] == "GOOGLE_SHEETS" and submission["review_status"] != "APPROVED":
            raise PermissionError("Google Sheets responses require an explicitly approved consent review")
        if row["profile_type"] != "owner_controlled_test" and not is_us_eligible(row["geographic_classification"]):
            raise PermissionError("GEO handoff requires VERIFIED_US geographic eligibility")
        if row["profile_type"] == "owner_controlled_test" and scope.get("fixture_protocol") != "OWNER_CONTROLLED_TEST_LOCAL_ONLY":
            raise PermissionError("owner-controlled test fixture requires explicit local fixture protocol")
        existing = db.execute("SELECT handoff_id FROM geo_handoffs WHERE profile_id=? AND profile_version=?", (profile_id, row["profile_version"])).fetchone()
        if existing:
            return existing["handoff_id"]
        handoff_id = "handoff-" + secrets.token_urlsafe(10)
        db.execute("INSERT INTO geo_handoffs(handoff_id,profile_id,profile_version,contract_version,consent_submission_id,scope_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (handoff_id, profile_id, row["profile_version"], CONTRACT_VERSION, submission_id, json.dumps(scope, sort_keys=True), "PENDING", now(), now()))
        db.execute("UPDATE research_records SET consent_status='CONSENTED',geo_research_status='QUEUED',updated_at=? WHERE profile_id=?", (now(), profile_id))
    return handoff_id


def withdraw_consent(profile_id: str, *, reason: str | None = None, path: Path = REGISTRY_DB) -> None:
    """Stop future research for a profile without deleting its evidence."""
    init_registry(path)
    with _connect(path) as db:
        changed = db.execute(
            "UPDATE research_records SET consent_status='WITHDRAWN',geo_research_status='BLOCKED_WITHDRAWAL',monitoring_status='BLOCKED_WITHDRAWAL',updated_at=? WHERE profile_id=?",
            (now(), profile_id),
        ).rowcount
        if not changed:
            raise ValueError("profile is not registered")


def classify_reply(text: str) -> str:
    lower = text.casefold()
    if any(word in lower for word in ("unsubscribe", "remove me", "do not contact")):
        return "UNSUBSCRIBE"
    if any(word in lower for word in ("no thanks", "decline", "not interested")):
        return "DECLINE"
    if any(word in lower for word in ("i consent", "consent", "yes, interested", "happy to participate")):
        return "EXPLICIT_CONSENT"
    if "?" in text or any(word in lower for word in ("what does", "how does", "question")):
        return "QUESTION"
    if any(word in lower for word in ("interested", "tell me more", "sounds good")):
        return "POSITIVE_INTEREST"
    return "UNCLEAR"


def create_email_draft(profile_id: str, recipient: str, body: str, *, path: Path = REGISTRY_DB, draft_version: str = "draft-v1", template_version: str = "research-invitation-v1") -> str:
    if not EMAIL_RE.match(recipient.strip()):
        raise ValueError("recipient must be a valid email address")
    init_registry(path)
    draft_id = "draft-" + secrets.token_urlsafe(10)
    with _connect(path) as db:
        record = db.execute("SELECT profile_type,geographic_classification FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        if not record:
            raise ValueError("profile is not registered")
        if record["profile_type"] != "owner_controlled_test" and not is_us_eligible(record["geographic_classification"]):
            raise PermissionError("email draft requires VERIFIED_US geographic eligibility")
        db.execute("INSERT INTO email_drafts(draft_id,profile_id,draft_version,template_version,recipient,preview_hash,body,approval_status,approved_by,approved_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (draft_id, profile_id, draft_version, template_version, recipient, _hash(body), body, "PENDING", None, None))
    return draft_id


MANUAL_TEST_RECIPIENTS = frozenset({
    "yuxuan.chen.1031@outlook.com",
    "yuxaun.chen.1031@outlook.com",
})


def _manual_observations(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only observations already persisted by the screening pipeline."""
    observations: list[dict[str, Any]] = []
    for item in record.get("screenings", [])[:1]:
        try:
            payload = json.loads(item.get("opportunities_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        values = payload.get("opportunities", []) if isinstance(payload, dict) else payload
        if isinstance(values, list):
            observations.extend(value for value in values[:2] if isinstance(value, dict))
    return observations[:2]


def create_manual_invitation_draft(
    profile_id: str,
    *,
    delivery_recipient: str,
    secret: str,
    path: Path = REGISTRY_DB,
    test_override: bool = False,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Create or return one reviewed, manual-send invitation draft.

    This function prepares a Word/Outlook-compatible draft only.  It never
    contacts a provider.  Test delivery addresses are explicit overrides and
    are never written into the source contact record.
    """
    recipient = delivery_recipient.strip().casefold()
    if not EMAIL_RE.match(recipient):
        raise ValueError("delivery recipient must be a valid email address")
    if test_override and recipient not in MANUAL_TEST_RECIPIENTS:
        raise PermissionError("test recipient is not on the owner-controlled allowlist")
    if not secret:
        raise RuntimeError("RESEARCH_QUESTIONNAIRE_SECRET is required")
    init_registry(path)
    record = get_record(profile_id, path)
    if record["profile_type"] != "owner_controlled_test":
        if not is_us_eligible(record["geographic_classification"]):
            raise PermissionError("manual invitation requires VERIFIED_US eligibility")
        if record["qualification_status"] not in {"ELIGIBLE", "QUALIFIED"}:
            raise PermissionError("company is not qualified for outreach")
    if record.get("suppression_status") == "SUPPRESSED" or record.get("withdrawal_status") == "WITHDRAWN":
        raise PermissionError("company is suppressed or withdrawn")

    # Prefer an explicitly verified official email, but never substitute a
    # representative or invitation address when it is absent.
    channels = record.get("contact_channels") or []
    official = sorted({str(item.get("email", "")).strip().casefold() for item in channels
                       if item.get("email") and str(item.get("review_status", "")).upper() in {"APPROVED", "VERIFIED", "CONTACT_VERIFIED"}
                       and EMAIL_RE.match(str(item.get("email", "")).strip())})
    intended = official[0] if len(official) == 1 else None
    if not test_override and not intended:
        raise PermissionError("no single verified official company email is available")

    with _connect(path) as db:
        existing = db.execute(
            "SELECT d.*,i.invitation_reference FROM email_drafts d LEFT JOIN questionnaire_invitations i ON i.invitation_id=d.invitation_id "
            "WHERE d.profile_id=? AND lower(COALESCE(d.delivery_recipient,d.recipient))=? "
            "AND d.approval_status IN ('PENDING','APPROVED') AND COALESCE(i.status,'INVITED') NOT IN ('SENT','EXPIRED') ORDER BY d.draft_id DESC LIMIT 1",
            (profile_id, recipient),
        ).fetchone()
        if existing:
            if test_override and not existing["test_override"]:
                with _connect(path) as update_db:
                    update_db.execute("UPDATE email_drafts SET intended_recipient=NULL,delivery_recipient=?,recipient_mode='TEST_OVERRIDE',test_override=1 WHERE draft_id=?", (recipient, existing["draft_id"]))
            return {"status": "EXISTING", "draft_id": existing["draft_id"], "invitation_id": existing["invitation_id"],
                    "invitation_reference": existing["invitation_reference"], "questionnaire_url": prefilled_form_url(existing["invitation_reference"]),
                    "subject": existing["subject"], "body": existing["body"], "delivery_recipient": recipient,
                    "intended_recipient": existing["intended_recipient"], "test_override": bool(existing["test_override"]),
                    "evidence": json.loads(existing["evidence_json"] or "[]")}

    invitation_id, _token = create_questionnaire_invitation(profile_id, secret=secret, path=path, expires_at=expires_at)
    reference = get_invitation_reference(invitation_id, path=path)
    form_url = prefilled_form_url(reference)
    observations = _manual_observations(record)
    evidence = []
    try:
        evidence.extend(json.loads(record.get("verification_evidence_json") or "[]"))
    except (TypeError, json.JSONDecodeError):
        pass
    evidence.extend({"type": "screening_observation", "observation": item, "source_urls": item.get("source_urls", [])} for item in observations)
    subject = f"GEO research participation invitation - {record['name']}"
    observation_lines = []
    for item in observations:
        title = item.get("title") or item.get("observation") or "Preliminary observation"
        urls = ", ".join(item.get("source_urls", [])) if isinstance(item.get("source_urls"), list) else ""
        observation_lines.append(f"- {title}{(' (' + urls + ')') if urls else ''}")
    if not observation_lines:
        observation_lines.append("- No preliminary GEO observation is established in the current registry.")
    label = "OWNER-CONTROLLED STAGING TEST - NOT PRODUCTION OUTREACH\n\n" if test_override else ""
    body = (f"{label}Hello,\n\n"
            f"We are inviting {record['name']} to participate in a research study about how public website information is represented in AI search. The website in this record is {record['official_website']}.\n\n"
            "Preliminary website observations already recorded in the research registry:\n" + "\n".join(observation_lines) + "\n\n"
            "These are observations and research hypotheses, not measured AI visibility results or guaranteed improvements. The proposed work is read-only public-website analysis; no website changes, experiments, or monitoring begin from this invitation.\n\n"
            f"Questionnaire: {form_url}\nInvitation reference: {reference}\n\n"
            "Please review the questionnaire carefully. Any response remains subject to human review and explicit authorization.\n\n"
            "Best regards\nGEO Research Outreach")
    draft_id = "draft-manual-" + secrets.token_urlsafe(12)
    with _connect(path) as db:
        db.execute("UPDATE email_drafts SET intended_recipient=?,delivery_recipient=?,recipient_mode=?,test_override=?,evidence_json=? WHERE invitation_id=?", (intended, recipient, "TEST_OVERRIDE" if test_override else "OFFICIAL_CONTACT", int(test_override), json.dumps(evidence, sort_keys=True), invitation_id))
        db.execute("INSERT INTO email_drafts(draft_id,profile_id,draft_version,template_version,recipient,preview_hash,body,approval_status,approved_by,approved_at,subject,invitation_id,intended_recipient,delivery_recipient,recipient_mode,test_override,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (draft_id, profile_id, "manual-draft-v1", "manual-invitation-v1", recipient, _hash(subject + "\n" + body), body, "PENDING", None, None, subject, invitation_id, intended, recipient, "TEST_OVERRIDE" if test_override else "OFFICIAL_CONTACT", int(test_override), json.dumps(evidence, sort_keys=True)))
    return {"status": "DRAFTED", "draft_id": draft_id, "invitation_id": invitation_id, "invitation_reference": reference,
            "questionnaire_url": form_url, "subject": subject, "body": body, "delivery_recipient": recipient,
            "intended_recipient": intended, "test_override": test_override, "evidence": evidence}


def reconcile_manual_send(draft_id: str, *, provider: str, provider_message_id: str, evidence_ref: str, sent_at: str | None = None, path: Path = REGISTRY_DB) -> dict[str, Any]:
    """Reconcile an already completed Outlook/Word send; never sends itself."""
    if not provider_message_id.strip() or not evidence_ref.strip():
        raise ValueError("provider message ID and Sent Items evidence reference are required")
    init_registry(path)
    with _connect(path) as db:
        draft = db.execute("SELECT * FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not draft or draft["approval_status"] != "APPROVED":
            raise PermissionError("an approved draft is required")
        key = "manual-provider:" + draft_id + ":" + provider_message_id.strip()
        prior = db.execute("SELECT event_id FROM email_events WHERE idempotency_key=?", (key,)).fetchone()
        if prior:
            return {"status": "EXISTING", "event_id": prior["event_id"], "draft_id": draft_id}
        event_id = "email-event-" + secrets.token_urlsafe(10)
        db.execute("INSERT INTO email_events(event_id,draft_id,event_type,provider,provider_enabled,message_id,error,created_at,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)", (event_id, draft_id, "MANUAL_PROVIDER_ACCEPTED", provider, 1, provider_message_id.strip(), evidence_ref.strip(), sent_at or now(), key))
        if draft["invitation_id"]:
            db.execute("UPDATE questionnaire_invitations SET status='SENT' WHERE invitation_id=? AND status NOT IN ('SENT','RESPONDED')", (draft["invitation_id"],))
        db.execute("UPDATE research_records SET recruitment_status='INVITED',questionnaire_status='INVITED',updated_at=? WHERE profile_id=?", (sent_at or now(), draft["profile_id"]))
    return {"status": "RECONCILED", "event_id": event_id, "draft_id": draft_id, "provider": provider, "provider_message_id": provider_message_id.strip()}


def create_post_form_confirmation_draft(submission_id: str, *, path: Path = REGISTRY_DB) -> dict[str, Any]:
    """Prepare, but never send, the formal post-form confirmation email.

    The destination must be an explicitly verified official company channel in
    the existing registry. Representative and invitation addresses are never
    substituted. Explicit authorization ``NO`` and quarantined submissions are
    hard blockers.
    """
    init_registry(path)
    confirmation_policy = configured_confirmation_policy()
    with _connect(path) as db:
        submission = db.execute("SELECT * FROM questionnaire_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("submission is not registered")
        invitation = db.execute("SELECT * FROM questionnaire_invitations WHERE invitation_id=?", (submission["invitation_id"],)).fetchone()
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (invitation["profile_id"],)).fetchone() if invitation else None
        if not invitation or not record:
            raise ValueError("submission is not linked to a registered profile")
        if submission["research_authorization"] != "YES":
            return {"status": "BLOCKED_EXPLICIT_AUTHORIZATION_NO", "submission_id": submission_id}
        if db.execute("SELECT 1 FROM questionnaire_conflicts WHERE invitation_id=? AND status='QUARANTINED'", (invitation["invitation_id"],)).fetchone():
            return {"status": "BLOCKED_CONFLICT", "submission_id": submission_id}
        if record["contact_verification_status"] != "CONTACT_VERIFIED":
            return {"status": "BLOCKED_NO_VERIFIED_OFFICIAL_EMAIL", "submission_id": submission_id}
        channels = json.loads(record["contact_channels_json"] or "[]")
        verified = [item for item in channels if item.get("email") and item.get("channel_type") in {"GENERAL_BUSINESS_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "PARTNERSHIP_EMAIL", "SALES_MARKETING_EMAIL", "NAMED_PERSON_EMAIL"} and str(item.get("review_status", "")).upper() in {"APPROVED", "VERIFIED", "CONTACT_VERIFIED"}]
        emails = sorted({str(item["email"]).strip().casefold() for item in verified if EMAIL_RE.match(str(item["email"]).strip())})
        if len(emails) != 1:
            return {"status": "BLOCKED_OFFICIAL_EMAIL_AMBIGUOUS" if len(emails) > 1 else "BLOCKED_NO_VERIFIED_OFFICIAL_EMAIL", "submission_id": submission_id}
        official_email = emails[0]
        existing = db.execute("SELECT confirmation_id,draft_id,status,official_company_email FROM confirmation_emails WHERE submission_id=?", (submission_id,)).fetchone()
        if existing:
            return {"status": "EXISTING", "confirmation_id": existing["confirmation_id"], "draft_id": existing["draft_id"], "official_company_email": existing["official_company_email"]}
        invitation_recipient = submission["invitation_recipient_email"] or ""
        representative_email = submission["representative_email"] or submission["contact_email"] or ""
        subject = f"Formal GEO research authorization confirmation - {record['name']}"
        if confirmation_policy == "AFFIRMATIVE_REPLY_REQUIRED":
            confirmation_instruction = (
                "Please confirm or dispute this authorization by replying to this email and identify invitation reference "
            )
        else:
            confirmation_instruction = (
                "This is a notice of the recorded authorization. If this is inaccurate, please dispute it by replying and identify invitation reference "
            )
        body = (
            f"Hello,\n\nThis is a formal confirmation {'request' if confirmation_policy == 'AFFIRMATIVE_REPLY_REQUIRED' else 'notice'} regarding {record['name']} and the public website GEO research invitation.\n\n"
            f"The submitted form identified {submission['representative_full_name'] or submission['respondent_name']} as the representative, with role {submission['representative_job_title'] or submission['respondent_role']}, and recorded representative email {representative_email}.\n\n"
            "The proposed scope is read-only analysis of the company's public website for GEO research. No website changes, publication, experiments, or monitoring will occur without separate approval.\n\n"
            + confirmation_instruction
            + f"{invitation['invitation_reference']}. This message is not proof of authorization, and no research will begin without explicit review.\n"
        )
        draft_id = "draft-confirmation-" + _hash(submission_id + official_email)[:20]
        confirmation_id = "confirmation-" + _hash(submission_id + official_email)[:20]
        db.execute("INSERT INTO email_drafts(draft_id,profile_id,draft_version,template_version,recipient,preview_hash,body,approval_status,approved_by,approved_at,subject,invitation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (draft_id, record["profile_id"], "confirmation-draft-v1", "post-form-confirmation-v1", official_email, _hash(subject + "\n" + body), body, "PENDING", None, None, subject, invitation["invitation_id"]))
        db.execute("INSERT INTO confirmation_emails(confirmation_id,profile_id,invitation_id,submission_id,draft_id,official_company_email,status,confirmation_policy,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (confirmation_id, record["profile_id"], invitation["invitation_id"], submission_id, draft_id, official_email, "DRAFT", confirmation_policy, now()))
        return {"status": "DRAFT", "confirmation_id": confirmation_id, "draft_id": draft_id, "official_company_email": official_email, "invitation_recipient_email": invitation_recipient, "representative_email": representative_email, "confirmation_policy": confirmation_policy, "subject": subject, "body": body}


def persist_combined_screening_and_draft(profile_id: str, *, result: dict[str, Any], recipient: str, secret: str, path: Path = REGISTRY_DB, base_url: str | None = None, expires_at: str | None = None) -> dict[str, str]:
    """Atomically persist one analysis envelope, secure invitation, and draft.

    The result must already contain the website evidence used for both the
    screening and the draft. Opening, editing, approving, or previewing the
    resulting draft never calls an AI provider.
    """
    if not EMAIL_RE.match(recipient.strip()):
        raise ValueError("recipient must be a valid email address")
    screening = result.get("screening") or {}
    rating = str(screening.get("rating") or "INSUFFICIENT_EVIDENCE").upper()
    if rating not in {"HIGH", "MEDIUM", "LOW", "INSUFFICIENT_EVIDENCE"}:
        raise ValueError("invalid GEO potential rating")
    result_text = json.dumps(result, sort_keys=True)
    content_hash = str(result.get("content_hash") or _hash(json.dumps(result.get("evidence") or [], sort_keys=True)))
    analysis_id = "analysis-" + _hash(profile_id + content_hash + str(result.get("mode", "RULE_BASED")))[:20]
    init_registry(path)
    raw = secrets.token_urlsafe(32)
    token_hash = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    invitation_id = "invite-" + secrets.token_urlsafe(10)
    draft_id = "draft-" + secrets.token_urlsafe(10)
    with _connect(path) as db:
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
        if not record:
            raise ValueError("profile is not registered")
        if record["profile_type"] == "owner_controlled_test":
            raise PermissionError("owner-controlled test fixture cannot receive normal recruitment")
        if not is_us_eligible(record["geographic_classification"]):
            raise PermissionError("combined screening requires VERIFIED_US geographic eligibility")
        if record["consent_status"] in {"DECLINED", "WITHDRAWN"} or record["recruitment_status"] in {"EXCLUDED", "UNSUBSCRIBED"}:
            raise PermissionError("profile is suppressed or excluded")
        prior = db.execute("SELECT analysis_id FROM screening_analysis_results WHERE profile_id=? AND content_hash=? AND mode=?", (profile_id, content_hash, str(result.get("mode", "RULE_BASED")))).fetchone()
        if prior:
            existing = db.execute("SELECT draft_id,invitation_id,body FROM email_drafts WHERE analysis_id=?", (prior["analysis_id"],)).fetchone()
            if existing:
                link_match = re.search(r"https://docs\.google\.com/forms/[^\s]+", existing["body"] or "") or re.search(r"https?://[^\s]+/questionnaire\?token=[^\s]+", existing["body"] or "")
                reference_row = db.execute("SELECT invitation_reference FROM questionnaire_invitations WHERE invitation_id=?", (existing["invitation_id"],)).fetchone()
                return {"analysis_id": prior["analysis_id"], "draft_id": existing["draft_id"], "invitation_id": existing["invitation_id"] or "", "invitation_reference": reference_row["invitation_reference"] if reference_row else "", "questionnaire_url": link_match.group(0).rstrip(")") if link_match else "", "status": "IDEMPOTENT_EXISTING"}
        db.execute("INSERT INTO screening_analysis_results(analysis_id,profile_id,profile_version,mode,content_hash,result_json,input_tokens,output_tokens,estimated_cost_usd,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (analysis_id, profile_id, record["profile_version"], str(result.get("mode", "RULE_BASED")), content_hash, result_text, int(result.get("input_tokens", 0)), int(result.get("output_tokens", 0)), float(result.get("estimated_cost_usd", 0)), now()))
        screening_id = "screening-" + _hash(analysis_id)[:20]
        db.execute("INSERT INTO screenings VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (screening_id) DO NOTHING", (screening_id, profile_id, record["profile_version"], "PRELIMINARY_UNVALIDATED", json.dumps(screening, sort_keys=True), json.dumps(result.get("evidence") or [], sort_keys=True), str(result.get("provider") or result.get("mode") or "RULE_BASED"), result.get("model"), result.get("prompt_version"), "combined-screening-and-draft/v1", now()))
        invitation_reference = make_invitation_reference(profile_id, nonce=invitation_id)
        db.execute("INSERT INTO questionnaire_invitations(invitation_id,profile_id,invitation_reference,token_hash,token_expires_at,status,created_at) VALUES (?,?,?,?,?,?,?)", (invitation_id, profile_id, invitation_reference, token_hash, expires_at, "DRAFTED", now()))
        subject = str(result.get("invitation", {}).get("subject") or f"Research invitation for {record['name']}")
        google_link = prefilled_form_url(invitation_reference)
        body = str(result.get("invitation", {}).get("body") or "").replace("{{questionnaire_url}}", google_link).replace("{QUESTIONNAIRE_URL}", google_link)
        if google_link not in body:
            raise ValueError("invitation body must include the Google Forms questionnaire URL")
        db.execute("INSERT INTO email_drafts(draft_id,profile_id,draft_version,template_version,recipient,preview_hash,body,approval_status,approved_by,approved_at,subject,analysis_id,invitation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (draft_id, profile_id, "combined-draft-v1", "research-invitation-combined-v1", recipient.strip(), _hash(subject + "\n" + body), body, "PENDING", None, None, subject, analysis_id, invitation_id))
        ready = "READY_FOR_EMAIL" if rating in {"HIGH", "MEDIUM"} else "NOT_READY"
        reasons = [] if ready == "READY_FOR_EMAIL" else [f"GEO_POTENTIAL_{rating}"]
        db.execute("UPDATE research_records SET screening_status='SCREENING_COMPLETE',screening_provider=?,screening_model=?,screening_prompt_version=?,screening_completed_at=?,outreach_ready_status=?,readiness_reason_json=?,recruitment_status='DRAFTED',questionnaire_status='DRAFTED',updated_at=? WHERE profile_id=?", (result.get("provider") or result.get("mode") or "RULE_BASED", result.get("model"), result.get("prompt_version"), now(), ready, json.dumps(reasons, sort_keys=True), now(), profile_id))
    return {"analysis_id": analysis_id, "draft_id": draft_id, "invitation_id": invitation_id, "invitation_reference": invitation_reference, "questionnaire_url": google_link, "status": "DRAFTED"}


def edit_email_draft(draft_id: str, *, body: str, subject: str | None = None, path: Path = REGISTRY_DB) -> None:
    if not body.strip():
        raise ValueError("draft body cannot be empty")
    init_registry(path)
    with _connect(path) as db:
        row = db.execute("SELECT subject FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not row:
            raise KeyError(draft_id)
        new_subject = subject if subject is not None else row["subject"]
        db.execute("UPDATE email_drafts SET subject=?,body=?,preview_hash=?,approval_status='PENDING',approved_by=NULL,approved_at=NULL WHERE draft_id=?", (new_subject, body, _hash(new_subject + "\n" + body), draft_id))


def approve_email(draft_id: str, reviewer: str, *, path: Path = REGISTRY_DB) -> None:
    with _connect(path) as db:
        changed = db.execute("UPDATE email_drafts SET approval_status='APPROVED',approved_by=?,approved_at=? WHERE draft_id=? AND approval_status='PENDING'", (reviewer, now(), draft_id)).rowcount
        if not changed:
            raise PermissionError("draft not found or already reviewed")


def suppress_recipient(recipient: str, *, reason: str = "unsubscribe", path: Path = REGISTRY_DB) -> None:
    if not EMAIL_RE.match(recipient.strip()):
        raise ValueError("recipient must be a valid email address")
    init_registry(path)
    with _connect(path) as db:
        db.execute("INSERT INTO suppressed_recipients VALUES (?,?,?) ON CONFLICT (recipient) DO UPDATE SET reason=EXCLUDED.reason,created_at=EXCLUDED.created_at", (recipient.casefold(), reason, now()))


def record_simulated_send(draft_id: str, *, expected_preview_hash: str | None = None, idempotency_key: str | None = None, path: Path = REGISTRY_DB) -> str:
    """Record a local simulation only; there is intentionally no provider call."""
    with _connect(path) as db:
        draft = db.execute("SELECT approval_status,preview_hash,recipient FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not draft or draft["approval_status"] != "APPROVED":
            raise PermissionError("human approval is required before sending")
        if expected_preview_hash is not None and expected_preview_hash != draft["preview_hash"]:
            raise ValueError("preview hash is stale")
        if db.execute("SELECT 1 FROM suppressed_recipients WHERE recipient=?", (draft["recipient"].casefold(),)).fetchone():
            raise PermissionError("recipient is suppressed")
        key = idempotency_key or "simulated-send:" + draft_id
        prior = db.execute("SELECT event_id FROM email_events WHERE idempotency_key=?", (key,)).fetchone()
        if prior:
            return prior["event_id"]
        event_id = "email-event-" + secrets.token_urlsafe(10)
        db.execute("INSERT INTO email_events(event_id,draft_id,event_type,provider,provider_enabled,message_id,error,created_at,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)", (event_id, draft_id, "SIMULATED_SEND", "disabled-provider", 0, None, "Production delivery is disabled", now(), key))
    return event_id


def ingest_candidate_stores(*, business_db: Path, hotel_db: Path, registry_db: Path = REGISTRY_DB) -> dict[str, int]:
    """Read both candidate stores and upsert source facts into the lifecycle registry.

    Source connections use SQLite read-only mode. Existing lifecycle fields are
    not reset on re-ingestion, so consent, drafts, replies, and handoffs remain
    authoritative in the registry.
    """
    init_registry(registry_db)
    counts = {"business_seen": 0, "hotel_seen": 0, "inserted": 0, "updated": 0}

    def ingest_one(source_db: Path, *, hotel: bool) -> None:
        uri = f"file:{source_db.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as source:
            source.row_factory = sqlite3.Row
            channel_table = "hotel_contact_channels" if hotel else "phase6f_contact_channels"
            channel_by_id: dict[str, list[dict[str, Any]]] = {}
            channel_columns = {row[1] for row in source.execute(f"PRAGMA table_info({channel_table})")}
            if channel_columns:
                for channel in source.execute(f"SELECT * FROM {channel_table}"):
                    item = dict(channel)
                    item["channel_type"] = item.get("channel_type") or "UNKNOWN"
                    item["source_url"] = item.get("source_url") or item.get("channel_url") or ""
                    channel_by_id.setdefault(str(item["candidate_id"]), []).append(item)
            screening_by_id: dict[str, dict[str, Any]] = {}
            if not hotel and source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase6e_screening'").fetchone():
                screening_by_id = {str(row["candidate_id"]): dict(row) for row in source.execute("SELECT * FROM phase6e_screening")}
            target_connection = _connect(registry_db)
            for candidate in source.execute("SELECT * FROM candidates"):
                row = dict(candidate)
                profile_id = str(row["candidate_id"])
                channels = channel_by_id.get(profile_id, [])
                suitable = [item for item in channels if str(item.get("review_status", "PENDING")).upper() != "REJECTED" and (item.get("email") or item.get("channel_url"))]
                evidence = json.loads(row.get("evidence_json") or "{}") if row.get("evidence_json") else {}
                provenance = [{"source_name": row.get("source_name"), "source_url": row.get("source_url"), "source_record_id": row.get("source_record_id"), "license_or_permission": row.get("source_license_or_permission"), "observed_at": row.get("discovery_timestamp")}]
                verification = [{"url": row.get("source_url"), "reason": row.get("verification_reason"), "last_inspected_at": row.get("last_inspected_at"), "status": row.get("verification_status")}]
                property_type = "hotel_property" if hotel else "business_company"
                cohort = {"hotel": hotel, "smb": False if hotel else False, "smb_exclusion_reason": "HOTEL_PROPERTY" if hotel else "SIZE_UNKNOWN_OR_NOT_RECONCILED"}
                source_screen = screening_by_id.get(profile_id, {})
                lifecycle = {
                    "profile_id": profile_id, "profile_version": PROFILE_VERSION, "profile_type": property_type,
                    "name": row.get("company_name") or profile_id, "official_website": row.get("website") or "",
                    "industry": row.get("industry") or "", "location": row.get("location") or "",
                    "size_status": "SIZE_UNKNOWN", "size_evidence_json": "[]", "verification_evidence_json": json.dumps(verification, sort_keys=True),
                    "provenance_json": json.dumps(provenance, sort_keys=True), "contact_channels_json": json.dumps(channels, sort_keys=True),
                    "cohort_membership_json": json.dumps(cohort, sort_keys=True), "identity_verification_status": row.get("verification_status") or "UNKNOWN",
                    "geographic_classification": classify_geography(location=row.get("location"), address=row.get("address"), source_name=row.get("source_name"), source_batch=row.get("source_batch"), profile_type=property_type)[0],
                    "geographic_evidence_json": json.dumps(classify_geography(location=row.get("location"), address=row.get("address"), source_name=row.get("source_name"), source_batch=row.get("source_batch"), profile_type=property_type)[1], sort_keys=True),
                    "contact_verification_status": "CONTACT_EVIDENCE_PRESENT" if suitable else "NOT_VERIFIED",
                    "screening_status": "NOT_SCREENED", "recruitment_priority": row.get("geo_opportunity_status") or "NEEDS_REVIEW",
                    "outreach_ready_status": "NOT_READY", "qualification_status": row.get("geo_eligibility_status") or "UNKNOWN",
                    "recruitment_status": "EXCLUDED" if not row.get("website") else "NOT_INVITED", "questionnaire_status": "NOT_INVITED",
                    "consent_status": "NOT_ASKED", "geo_property_id": None, "geo_research_status": "AWAITING_CONSENT",
                    "monitoring_status": "NOT_STARTED", "created_at": row.get("discovery_timestamp") or now(), "updated_at": now(),
                }
                with nullcontext(target_connection) as target:
                    existing = target.execute("SELECT created_at,recruitment_status,questionnaire_status,consent_status,geo_research_status,monitoring_status FROM research_records WHERE profile_id=?", (profile_id,)).fetchone()
                    if existing:
                        lifecycle["created_at"] = existing["created_at"]
                        target.execute("""UPDATE research_records SET profile_version=:profile_version,profile_type=:profile_type,name=:name,official_website=:official_website,industry=:industry,location=:location,size_status=:size_status,size_evidence_json=:size_evidence_json,verification_evidence_json=:verification_evidence_json,provenance_json=:provenance_json,contact_channels_json=:contact_channels_json,cohort_membership_json=:cohort_membership_json,identity_verification_status=:identity_verification_status,geographic_classification=:geographic_classification,geographic_evidence_json=:geographic_evidence_json,contact_verification_status=:contact_verification_status,screening_status=:screening_status,recruitment_priority=:recruitment_priority,outreach_ready_status=:outreach_ready_status,qualification_status=:qualification_status,updated_at=:updated_at WHERE profile_id=:profile_id""", lifecycle)
                        counts["updated"] += 1
                    else:
                        target.execute("""INSERT INTO research_records
                          (profile_id,profile_version,profile_type,name,official_website,industry,location,size_status,size_evidence_json,verification_evidence_json,provenance_json,contact_channels_json,cohort_membership_json,identity_verification_status,geographic_classification,geographic_evidence_json,contact_verification_status,screening_status,recruitment_priority,outreach_ready_status,qualification_status,recruitment_status,questionnaire_status,consent_status,geo_property_id,geo_research_status,monitoring_status,created_at,updated_at)
                          VALUES (:profile_id,:profile_version,:profile_type,:name,:official_website,:industry,:location,:size_status,:size_evidence_json,:verification_evidence_json,:provenance_json,:contact_channels_json,:cohort_membership_json,:identity_verification_status,:geographic_classification,:geographic_evidence_json,:contact_verification_status,:screening_status,:recruitment_priority,:outreach_ready_status,:qualification_status,:recruitment_status,:questionnaire_status,:consent_status,:geo_property_id,:geo_research_status,:monitoring_status,:created_at,:updated_at)""", lifecycle)
                        counts["inserted"] += 1
                counts["hotel_seen" if hotel else "business_seen"] += 1
            target_connection.commit()
            target_connection.close()

    ingest_one(business_db, hotel=False)
    ingest_one(hotel_db, hotel=True)
    return counts


def list_records(path: Path = REGISTRY_DB, *, q: str = "", profile_type: str = "", consent_status: str = "", identity_status: str = "", contact_status: str = "", screening_status: str = "", recruitment_status: str = "", geo_status: str = "", geographic_status: str = "", promotion_state: str = "", industry: str = "", location: str = "", sort: str = "name", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    init_registry(path)
    clauses, args = [], []
    if q:
        clauses.append("(name LIKE ? OR official_website LIKE ? OR profile_id LIKE ?)"); args.extend([f"%{q}%"] * 3)
    if profile_type:
        clauses.append("profile_type=?"); args.append(profile_type)
    if consent_status:
        clauses.append("consent_status=?"); args.append(consent_status)
    for field, value in (("identity_verification_status", identity_status), ("contact_verification_status", contact_status), ("screening_status", screening_status), ("recruitment_status", recruitment_status), ("geo_research_status", geo_status), ("geographic_classification", geographic_status)):
        if value:
            clauses.append(field + "=?"); args.append(value)
    if promotion_state == "OPERATIONAL":
        clauses.append("promotion_state IN ('PROSPECT','OUTREACH_READY','INVITED','RESEARCH_PARTICIPANT')")
    elif promotion_state == "OPERATIONAL_WITH_TEST":
        clauses.append("(promotion_state IN ('PROSPECT','OUTREACH_READY','INVITED','RESEARCH_PARTICIPANT') OR profile_type='owner_controlled_test')")
    elif promotion_state:
        clauses.append("promotion_state=?"); args.append(promotion_state)
    for field, value in (("industry", industry), ("location", location)):
        if value:
            clauses.append(field + " LIKE ?"); args.append("%" + value + "%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    # SQLite's NOCASE collation is not available on PostgreSQL. PostgreSQL's
    # default text ordering is deterministic enough for the cloud read model;
    # keep the historical collation only for the local SQLite registry.
    default_name_order = "name" if is_postgres_target(path) else "name COLLATE NOCASE"
    order = {"name": default_name_order, "identity": "identity_verification_status", "consent": "consent_status", "updated": "updated_at DESC"}.get(sort, default_name_order)
    with _connect(path) as db:
        total = db.execute("SELECT COUNT(*) FROM research_records" + where, args).fetchone()[0]
        rows = [dict(row) for row in db.execute("SELECT * FROM research_records" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [max(1, min(limit, 100)), max(0, offset)])]
    return {"records": rows, "total": total, "limit": limit, "offset": offset, "sort": sort}


def export_records_csv(path: Path = REGISTRY_DB, **filters: str) -> str:
    result = list_records(path, **filters)
    output = io.StringIO(); rows = result["records"]
    fields = ["profile_id", "profile_type", "name", "official_website", "industry", "location", "geographic_classification", "recruitment_status", "questionnaire_status", "consent_status", "geo_research_status", "monitoring_status"]
    writer = csv.DictWriter(output, fieldnames=fields); writer.writeheader(); writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    return output.getvalue()

