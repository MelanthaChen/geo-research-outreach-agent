"""Local, read-only Phase 1 adapter for the GEO Engine contract.

This module is deliberately an adapter, not an audit or experiment runner.  It
keeps the source profile immutable, emits versioned records, and stores result
records append-only with content-based idempotency.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from bs4 import BeautifulSoup

CONTRACT_VERSION = "geo-research-integration/v1"
PROFILE_VERSION = "geo-company-profile/v1"
OWNER_CONTROLLED_TEST = "OWNER_CONTROLLED_TEST"
TEST_SITE_ID = "geoairesume-owner-test-v1"
TEST_SITE_URL = "https://geo-ai-resume.vercel.app"
TEST_REGISTRY_PATH = Path("data/owner_controlled_test_sites.jsonl")

CHANNEL_EMAIL_ONLY = "EMAIL_ONLY"
CHANNEL_FORM_ONLY = "FORM_ONLY"
CHANNEL_EMAIL_AND_FORM = "EMAIL_AND_FORM"
CHANNEL_NO_CONTACT = "NO_CONTACT_CHANNEL"

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _record_id(record_type: str, profile_id: str, payload: Any) -> str:
    return f"{record_type}:{profile_id}:{content_hash(payload)[7:19]}"


def _require(value: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise ValueError(f"missing required integration fields: {', '.join(missing)}")


def _profile_type(profile: dict[str, Any]) -> str:
    raw = str(profile.get("profile_type", ""))
    return {"hotel_property": "hotel_property", "startup_company": "business_company", "owner_controlled_test": "owner_controlled_test"}.get(raw, "business_company")


def build_profile_import(profile: dict[str, Any], *, received_at: str | None = None) -> dict[str, Any]:
    """Map an existing ``geo-company-profile/v1`` record to the GEO Engine import schema."""
    if profile.get("schema_version") != PROFILE_VERSION:
        raise ValueError(f"expected {PROFILE_VERSION}")
    identity = profile.get("identity") or {}
    profile_id = str(profile.get("profile_id") or "")
    domain = identity.get("website") or identity.get("website_url") or ""
    if not profile_id or not domain:
        raise ValueError("profile_id and identity.website are required")
    if profile.get("profile_type") == "owner_controlled_test":
        profile_type = "owner_controlled_test"
        memberships: list[str] = []
        exclusion_reason = "CONTROLLED_TEST_FIXTURE"
    else:
        profile_type = _profile_type(profile)
        membership = profile.get("cohort_membership") or {}
        memberships = []
        if membership.get("smb") is True or membership.get("smb") == "YES":
            memberships.append("SMB_COHORT")
        if membership.get("hotel") is True or membership.get("hotel") == "YES":
            memberships.append("HOTEL_COHORT")
        exclusion_reason = None
    snapshot = dict(profile)
    record = {
        "contract_version": CONTRACT_VERSION,
        "record_type": "profile_import",
        "schema_version": CONTRACT_VERSION,
        "source_system": "geo-research-outreach-agent",
        "source_schema_version": PROFILE_VERSION,
        "source_profile_id": profile_id,
        "source_profile_version": PROFILE_VERSION,
        "profile_type": profile_type,
        "official_name": identity.get("name", profile_id),
        "official_domain": domain,
        "formal_cohort_memberships": memberships,
        "property_id": None,
        "exclusion_reason": exclusion_reason,
        "profile_snapshot": snapshot,
        "received_at": received_at or utc_now(),
        "provenance": {
            "source_system": "geo-research-outreach-agent",
            "source_profile_id": profile_id,
            "source_schema_version": PROFILE_VERSION,
            "source_record_hash": content_hash(profile),
        },
    }
    record["record_id"] = _record_id("profile_import", profile_id, record)
    validate_integration_record(record)
    return record


def make_owner_controlled_test_registry_record() -> dict[str, Any]:
    return {
        "schema_version": "outreach-test-site/v1",
        "test_site_id": TEST_SITE_ID,
        "record_classification": OWNER_CONTROLLED_TEST,
        "formal_cohort_memberships": [],
        "site_name": "GeoAIResume controlled test website",
        "canonical_url": TEST_SITE_URL,
        "owner_authorization": {"status": "PENDING_EXPLICIT_CONFIRMATION", "scope": "read-only contact-channel discovery", "confirmed_at": None, "evidence_ref": None},
        "allowed_actions": ["FETCH_PUBLIC_PAGES", "DETECT_EMAIL_TEXT", "DETECT_FORM_CONTROLS"],
        "prohibited_actions": ["SUBMIT_FORM", "SEND_EMAIL", "CHANGE_WEBSITE", "COUNT_TOWARD_SMB_TARGET", "COUNT_TOWARD_HOTEL_TARGET", "CAMPAIGN_ELIGIBILITY", "NORMAL_COMPANY_OUTREACH"],
        "contact_evidence_status": "PENDING",
    }


def write_owner_controlled_test_registry(path: Path = TEST_REGISTRY_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(make_owner_controlled_test_registry_record()) + "\n", encoding="utf-8")
    return path


def classify_contact_channels(channels: Iterable[dict[str, Any]]) -> str:
    rows = list(channels)
    has_email = any(row.get("channel_type") == "EMAIL" and row.get("value") for row in rows)
    has_form = any(row.get("channel_type") == "FORM" and row.get("form_controls_observed") is True for row in rows)
    if has_email and has_form:
        return CHANNEL_EMAIL_AND_FORM
    if has_email:
        return CHANNEL_EMAIL_ONLY
    if has_form:
        return CHANNEL_FORM_ONLY
    return CHANNEL_NO_CONTACT


def discover_public_contact(*, source_url: str, html: str, captured_at: str | None = None, review_status: str = "PENDING") -> dict[str, Any]:
    """Parse supplied public HTML only; this function never fetches or submits."""
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("source_url must be HTTP(S)")
    soup = BeautifulSoup(html, "html.parser")
    visible_text = " ".join(soup.stripped_strings)
    emails = sorted(set(EMAIL_RE.findall(visible_text)))
    form = soup.find("form")
    controls = bool(form and (form.find(["input", "textarea", "button", "select"]) or form.get("action")))
    channels = []
    for email in emails:
        channels.append({"channel_type": "EMAIL", "value": email, "source_url": source_url, "evidence_text": email, "captured_at": captured_at or utc_now(), "review_status": review_status, "first_party": True})
    if form is not None:
        channels.append({"channel_type": "FORM", "value": None, "source_url": source_url, "evidence_text": str(form)[:500], "captured_at": captured_at or utc_now(), "review_status": review_status, "first_party": True, "form_controls_observed": controls})
    return {"source_url": source_url, "evidence_text": visible_text[:1000], "captured_at": captured_at or utc_now(), "channel_type": classify_contact_channels(channels), "channels": channels, "read_only": True}


def validate_integration_record(record: dict[str, Any]) -> None:
    _require(record, "contract_version", "record_type", "schema_version", "record_id", "source_profile_id", "source_profile_version", "provenance")
    if record["contract_version"] != CONTRACT_VERSION or record["schema_version"] != CONTRACT_VERSION:
        raise ValueError("unsupported integration contract version")
    if not record["source_profile_id"]:
        raise ValueError("source_profile_id must be non-empty")
    if record["record_type"] == "ranked_strategy_recommendation" and len(record.get("strategies", [])) > 3:
        raise ValueError("at most three strategies are allowed")


_REQUIRED_BY_TYPE = {
    "property_identity_mapping": ("property_id", "mapped_at"),
    "audit_result_reference": ("audit_id", "status", "audit_url", "page_refs", "captured_at"),
    "ranked_strategy_recommendation": ("audit_id", "ranking_policy", "strategies", "created_at"),
    "exact_text_edit_proposal": ("page_id", "proposal_id", "approval_status", "evidence_refs", "created_at"),
    "evaluation_question_set": ("questions", "created_at"),
    "baseline_experiment_reference": ("experiment_id", "status", "results_present", "created_at"),
    "deployment_receipt": ("proposal_id", "approval_status", "source_git_revision", "content_hash", "deployed_git_revision", "vercel_deployment_id", "public_url", "verification_status", "baseline_experiment_reference", "post_deployment_experiment_reference", "recorded_at"),
    "post_deployment_evaluation": ("deployment_id", "baseline_reference", "post_reference", "status", "results_present", "created_at"),
    "research_result_export": ("audit_reference", "strategies", "question_set_reference", "baseline_reference", "post_deployment_reference", "exported_at"),
}


def _records_from_result(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    _require(envelope, "contract_version", "source_profile_id", "source_profile_version", "property_id")
    if envelope["contract_version"] != CONTRACT_VERSION:
        raise ValueError("unsupported result contract version")
    profile_id = str(envelope["source_profile_id"])
    base = {"contract_version": CONTRACT_VERSION, "schema_version": CONTRACT_VERSION, "source_profile_id": profile_id, "source_profile_version": envelope["source_profile_version"], "property_id": envelope["property_id"], "provenance": envelope.get("provenance", {})}
    mappings = [("property_mapping", "property_identity_mapping"), ("audit", "audit_result_reference"), ("strategies", "ranked_strategy_recommendation"), ("exact_text_edits", "exact_text_edit_proposal"), ("question_set", "evaluation_question_set"), ("baseline", "baseline_experiment_reference"), ("deployment_receipts", "deployment_receipt"), ("post_deployment", "post_deployment_evaluation")]
    records: list[dict[str, Any]] = []
    for key, record_type in mappings:
        values = envelope.get(key)
        if values is None:
            continue
        values = values if isinstance(values, list) else [values]
        for value in values:
            record = dict(base)
            record.update(value)
            record["record_type"] = record_type
            record["record_id"] = str(record.get("record_id") or _record_id(record_type, profile_id, value))
            # Do not inject a new wall-clock value during retries. Callers that
            # need a creation timestamp must provide it (as the GEO Engine
            # schemas require); audit records already carry captured_at.
            if envelope.get("received_at"):
                record.setdefault("created_at", envelope["received_at"])
                record.setdefault("captured_at", envelope["received_at"])
            _require(record, *_REQUIRED_BY_TYPE.get(record_type, ()))
            validate_integration_record(record)
            records.append(record)
    if envelope.get("report") is not None:
        record = dict(base)
        record.update({"record_type": "research_result_export", "record_id": envelope.get("record_id") or _record_id("research_result_export", profile_id, envelope), "audit_reference": envelope.get("audit"), "strategies": envelope.get("strategies", []), "question_set_reference": envelope.get("question_set"), "baseline_reference": envelope.get("baseline"), "post_deployment_reference": envelope.get("post_deployment"), "report": envelope["report"], "exported_at": envelope.get("received_at") or utc_now()})
        validate_integration_record(record)
        records.append(record)
    return records


def import_result_envelope(envelope: dict[str, Any], path: Path) -> dict[str, int]:
    """Append result records; identical record IDs/content are safe retries."""
    records = _records_from_result(envelope)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                existing[row["record_id"]] = content_hash(row)
    added = skipped = 0
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            digest = content_hash(record)
            if record["record_id"] in existing:
                if existing[record["record_id"]] != digest:
                    raise ValueError(f"conflicting retry for record_id {record['record_id']}")
                skipped += 1
                continue
            handle.write(canonical_json(record) + "\n")
            added += 1
    return {"added": added, "skipped": skipped, "records": len(records)}

