"""Verified-consent handoff from the Outreach registry to GEO Engine.

This module is intentionally limited to property registration and identity
mapping. It does not run audits, experiments, deployments, monitoring, or
paid evaluation jobs. A bridge is injected in tests; production uses the
existing GEO Engine integration adapter through ``GEO_ENGINE_ROOT``.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from outreach_agent.geographic_eligibility import is_us_eligible
from outreach_agent.integration import CONTRACT_VERSION, PROFILE_VERSION
from outreach_agent.research_lifecycle import CONSENTED, CONSENT_VERSION, _connect, _hash, init_registry, now


class PropertyBridge(Protocol):
    def import_profile(self, profile: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class HttpGeoEngineBridge:
    """Authenticated cloud bridge to GEO Engine's existing adapter endpoint."""

    api_url: str
    token: str
    timeout_seconds: float = 20.0

    def import_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = _handoff_headers(self.token)
        response = httpx.post(
            self.api_url.rstrip("/") + "/api/v1/integration/profile-import",
            json=profile,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GEO Engine handoff failed ({response.status_code})")
        payload = response.json()
        if "property_id" not in payload:
            raise RuntimeError("GEO Engine handoff response omitted property_id")
        return payload

    def get_research_result(self, source_profile_id: str, source_profile_version: str | None = None) -> dict[str, Any]:
        import httpx

        params = {"source_profile_version": source_profile_version} if source_profile_version else {}
        response = httpx.get(
            self.api_url.rstrip("/") + f"/api/v1/integration/research-results/{source_profile_id}",
            params=params,
            headers=_handoff_headers(self.token),
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GEO Engine result retrieval failed ({response.status_code})")
        return response.json()

    def run_controlled_research(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Run only the GEO Engine's explicit owner-fixture route."""
        import httpx

        response = httpx.post(
            self.api_url.rstrip("/") + "/api/v1/integration/research-run",
            json=profile,
            headers=_handoff_headers(self.token),
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GEO Engine controlled research failed ({response.status_code})")
        return response.json()


def _handoff_headers(token: str) -> dict[str, str]:
    """Build the authenticated bridge headers.

    Authorization is the canonical contract. The explicit integration header
    is sent as a compatibility fallback for managed proxies that strip or
    rewrite Authorization headers; GEO Engine accepts either header but still
    validates the same shared secret.
    """
    return {
        "Authorization": f"Bearer {token}",
        "X-GEO-Engine-Handoff-Token": token,
    }


@dataclass
class LocalGeoEngineBridge:
    """Direct local bridge to GEO Engine's existing SQLAlchemy adapter.

    The GEO Engine database URL and credentials remain owned by GEO Engine.
    No HTTP endpoint or second property store is introduced.
    """

    engine_root: Path
    store_root: Path

    def import_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        backend = self.engine_root / "backend"
        if not (backend / "app" / "integration" / "adapter.py").is_file():
            raise RuntimeError(f"GEO Engine backend not found under {self.engine_root}")
        sys.path.insert(0, str(backend))
        try:
            from app.core.database import SessionLocal
            from app.integration.adapter import GeoResearchIntegrationAdapter

            with SessionLocal() as db:
                return GeoResearchIntegrationAdapter(db, self.store_root).import_profile(profile)
        finally:
            try:
                sys.path.remove(str(backend))
            except ValueError:
                pass


def default_bridge() -> PropertyBridge:
    api_url = os.environ.get("GEO_ENGINE_API_URL")
    token = os.environ.get("GEO_ENGINE_HANDOFF_TOKEN")
    if api_url or token:
        if not api_url or not token:
            raise RuntimeError("GEO_ENGINE_API_URL and GEO_ENGINE_HANDOFF_TOKEN must be configured together")
        return HttpGeoEngineBridge(api_url, token)
    root = os.environ.get("GEO_ENGINE_ROOT")
    if not root:
        raise RuntimeError("GEO_ENGINE_ROOT is required for automatic GEO Engine handoff")
    store = Path(os.environ.get("GEO_ENGINE_INTEGRATION_STORE", str(Path(root) / ".integration-data")))
    return LocalGeoEngineBridge(Path(root), store)


def _profile_for_handoff(record: Any, submission: Any) -> dict[str, Any]:
    provenance = json.loads(record["provenance_json"] or "{}")
    cohorts = json.loads(record["cohort_membership_json"] or "{}")
    evidence = json.loads(record["verification_evidence_json"] or "[]")
    profile: dict[str, Any] = {
        "schema_version": PROFILE_VERSION,
        "profile_id": record["profile_id"],
        "profile_type": record["profile_type"],
        "identity": {
            "name": record["name"],
            "website": record["official_website"],
            "verification_status": record["identity_verification_status"],
            "verification_evidence": evidence,
        },
        "cohort_membership": cohorts,
        "provenance": provenance,
        "qualification": {"eligibility": record["qualification_status"]},
        "startup": {"location": record["location"] or "", "industry": record["industry"] or ""},
        "consent": {
            "status": "VERIFIED",
            "consent_id": submission["submission_id"],
            "version": submission["consent_wording_version"],
            "research_scope": ["public_website_analysis"],
            "valid_until": None,
        },
    }
    return profile


OWNER_TEST_AUTOMATION_PROFILES = frozenset({
    "geoairesume-owner-test-v1",
    "whisker-haven-owner-demo-20261023-v1",
})


def owner_test_automation_enabled(profile_id: str) -> bool:
    """Fail closed unless the staging-only switch and exact IDs both match."""
    return (
        os.environ.get("OUTREACH_OWNER_TEST_AUTOMATION_ENABLED", "false").lower() == "true"
        and profile_id in OWNER_TEST_AUTOMATION_PROFILES
    )


def start_controlled_research(submission_id: str, *, path: Path, bridge: PropertyBridge | None = None) -> dict[str, Any]:
    """Start the existing controlled GEO run for the two staging fixtures only."""
    init_registry(path)
    with _connect(path) as db:
        submission = db.execute("SELECT * FROM questionnaire_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        invitation = db.execute("SELECT * FROM questionnaire_invitations WHERE invitation_id=?", (submission["invitation_id"],)).fetchone() if submission else None
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (invitation["profile_id"],)).fetchone() if invitation else None
        if not submission or not invitation or not record:
            raise ValueError("controlled research submission is not linked to a registered profile")
        if not owner_test_automation_enabled(record["profile_id"]):
            raise PermissionError("controlled automation is not enabled for this profile")
        if record["profile_type"] != "owner_controlled_test" or submission["review_status"] != "APPROVED" or submission["consent_status"] != CONSENTED:
            raise PermissionError("controlled research requires an approved owner test consent")
        handoff = db.execute("SELECT * FROM geo_handoffs WHERE profile_id=? AND profile_version=?", (record["profile_id"], record["profile_version"])).fetchone()
        if not handoff or handoff["status"] != "HANDED_OFF" or not handoff["property_id"]:
            raise PermissionError("controlled research requires a completed GEO handoff")
        if handoff["research_run_status"] == "COMPLETED":
            return {"status": "COMPLETED", "idempotent": True, "property_id": handoff["property_id"], "result_record_id": handoff["research_result_record_id"]}
        if record["withdrawal_status"] == "WITHDRAWN":
            raise PermissionError("controlled research is blocked by withdrawal")
        db.execute("UPDATE geo_handoffs SET research_run_status='RUNNING',research_started_at=?,research_error=NULL,updated_at=? WHERE handoff_id=?", (now(), now(), handoff["handoff_id"]))
        db.execute("UPDATE research_records SET geo_research_status='RESEARCH_RUNNING',updated_at=? WHERE profile_id=?", (now(), record["profile_id"]))
        db.commit()
        profile = _profile_for_handoff(record, submission)
        profile.update({"property_id": handoff["property_id"], "controlled_fixture": True})
    try:
        runner = getattr(bridge or default_bridge(), "run_controlled_research", None)
        if runner is None:
            raise RuntimeError("configured GEO Engine bridge does not support controlled research")
        result = runner(profile)
        nested = result.get("result") if isinstance(result, dict) else None
        result_record_id = (nested or {}).get("record_id") if isinstance(nested, dict) else None
        result_record_id = result_record_id or (result.get("record_id") if isinstance(result, dict) else None)
    except Exception as exc:
        with _connect(path) as db:
            db.execute("UPDATE geo_handoffs SET research_run_status='FAILED',research_error=?,updated_at=? WHERE handoff_id=?", (str(exc), now(), handoff["handoff_id"]))
            db.execute("UPDATE research_records SET geo_research_status='RESEARCH_FAILED',geo_integration_error=?,updated_at=? WHERE profile_id=?", (str(exc), now(), record["profile_id"]))
        return {"status": "FAILED", "property_id": handoff["property_id"], "error": str(exc), "retryable": True}
    with _connect(path) as db:
        db.execute("UPDATE geo_handoffs SET research_run_status='COMPLETED',research_result_record_id=?,research_completed_at=?,updated_at=? WHERE handoff_id=?", (result_record_id, now(), now(), handoff["handoff_id"]))
        db.execute("UPDATE research_records SET geo_research_status='RESEARCH_COMPLETED',updated_at=? WHERE profile_id=?", (now(), record["profile_id"]))
    return {"status": "COMPLETED", "idempotent": False, "property_id": handoff["property_id"], "result_record_id": result_record_id, "result": result}


def process_geo_handoff(submission_id: str, *, path: Path, bridge: PropertyBridge | None = None) -> dict[str, Any]:
    """Retry-safely register one explicitly approved submission as a Property."""
    init_registry(path)
    with _connect(path) as db:
        submission = db.execute("SELECT * FROM questionnaire_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("submission is not registered")
        invitation = db.execute("SELECT * FROM questionnaire_invitations WHERE invitation_id=?", (submission["invitation_id"],)).fetchone()
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (invitation["profile_id"],)).fetchone() if invitation else None
        if not invitation or not record:
            raise ValueError("submission is not linked to a registered profile")
        conflict = db.execute("SELECT 1 FROM questionnaire_conflicts WHERE invitation_id=? AND status='QUARANTINED'", (invitation["invitation_id"],)).fetchone()
        if conflict:
            raise PermissionError("handoff is blocked by a quarantined conflicting response")
        if record["consent_status"] == "WITHDRAWN" or submission["consent_status"] == "WITHDRAWN":
            db.execute("UPDATE research_records SET geo_research_status='BLOCKED_WITHDRAWN',geo_integration_status='WITHDRAWN',updated_at=? WHERE profile_id=?", (now(), record["profile_id"]))
            raise PermissionError("handoff is blocked by withdrawal")
        if submission["review_status"] != "APPROVED" or submission["consent_status"] != CONSENTED:
            raise PermissionError("handoff requires an approved verified-consent review")
        if submission["consent_wording_version"] != CONSENT_VERSION:
            raise PermissionError("unsupported consent wording version")
        if record["profile_type"] != "owner_controlled_test" and not is_us_eligible(record["geographic_classification"]):
            raise PermissionError("handoff requires VERIFIED_US geographic eligibility")
        existing = db.execute("SELECT * FROM geo_handoffs WHERE profile_id=? AND profile_version=?", (record["profile_id"], record["profile_version"])).fetchone()
        if existing and existing["status"] == "HANDED_OFF" and existing["property_id"]:
            return {"status": "HANDED_OFF", "handoff_id": existing["handoff_id"], "property_id": existing["property_id"], "idempotent": True}
        handoff_id = existing["handoff_id"] if existing else "handoff-" + secrets.token_urlsafe(10)
        if not existing:
            db.execute("INSERT INTO geo_handoffs(handoff_id,profile_id,profile_version,contract_version,consent_submission_id,scope_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (handoff_id, record["profile_id"], record["profile_version"], CONTRACT_VERSION, submission_id, json.dumps({"analysis": "website-only"}, sort_keys=True), "HANDOFF_PENDING", now(), now()))
        else:
            db.execute("UPDATE geo_handoffs SET status='HANDOFF_PENDING',consent_submission_id=?,updated_at=?,last_error=NULL WHERE handoff_id=?", (submission_id, now(), handoff_id))
        db.execute("UPDATE research_records SET geo_research_status='HANDOFF_PENDING',geo_integration_status='HANDOFF_PENDING',geo_integration_error=NULL,updated_at=? WHERE profile_id=?", (now(), record["profile_id"]))
        db.commit()
        profile = _profile_for_handoff(record, submission)
        attempt_id = "attempt-" + secrets.token_urlsafe(10)
        key = f"{record['profile_id']}:{record['profile_version']}:{submission_id}"
    try:
        result = (bridge or default_bridge()).import_profile(profile)
        property_id = int(result["property_id"])
    except Exception as exc:
        with _connect(path) as db:
            db.execute("UPDATE geo_handoffs SET status='HANDOFF_FAILED',attempt_count=attempt_count+1,last_error=?,updated_at=? WHERE handoff_id=?", (str(exc), now(), handoff_id))
            db.execute("INSERT INTO geo_handoff_attempts(attempt_id,handoff_id,profile_id,submission_id,idempotency_key,status,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (attempt_id, handoff_id, profile["profile_id"], submission_id, key, "FAILED", str(exc), now(), now()))
            db.execute("UPDATE research_records SET geo_research_status='HANDOFF_FAILED',geo_integration_status='HANDOFF_FAILED',geo_integration_error=?,updated_at=? WHERE profile_id=?", (str(exc), now(), profile["profile_id"]))
        return {"status": "HANDOFF_FAILED", "handoff_id": handoff_id, "error": str(exc), "retryable": True}
    with _connect(path) as db:
        db.execute("UPDATE geo_handoffs SET status='HANDED_OFF',property_id=?,attempt_count=attempt_count+1,last_error=NULL,updated_at=? WHERE handoff_id=?", (property_id, now(), handoff_id))
        db.execute("INSERT INTO geo_handoff_attempts(attempt_id,handoff_id,profile_id,submission_id,idempotency_key,status,property_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (attempt_id, handoff_id, profile["profile_id"], submission_id, key, "HANDED_OFF", property_id, now(), now()))
        db.execute("UPDATE research_records SET geo_property_id=?,geo_research_status='HANDED_OFF',geo_integration_status='HANDED_OFF',geo_integration_error=NULL,updated_at=? WHERE profile_id=?", (property_id, now(), profile["profile_id"]))
    return {"status": "HANDED_OFF", "handoff_id": handoff_id, "property_id": property_id, "idempotent": False}


def review_questionnaire_submission(submission_id: str, *, reviewer: str, company_identity_verified: bool, representative_authorized: bool, consent_version_verified: bool, website_scope_verified: bool, withdrawal_checked: bool, approve: bool, notes: str = "", path: Path, bridge: PropertyBridge | None = None) -> dict[str, Any]:
    """Record the human decision; approval is never inferred from form values."""
    init_registry(path)
    with _connect(path) as db:
        submission = db.execute("SELECT * FROM questionnaire_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("submission is not registered")
        invitation = db.execute("SELECT * FROM questionnaire_invitations WHERE invitation_id=?", (submission["invitation_id"],)).fetchone()
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (invitation["profile_id"],)).fetchone() if invitation else None
        if not invitation or not record:
            raise ValueError("submission is not linked to a registered profile")
        conflict = db.execute("SELECT 1 FROM questionnaire_conflicts WHERE invitation_id=? AND status='QUARANTINED'", (invitation["invitation_id"],)).fetchone()
        # The form's representative email identifies the accountable respondent;
        # it is not required to equal the original invitation recipient. The
        # reviewer verifies representative authorization independently. A
        # separate distinct submission for the invitation remains quarantined.
        checks = all((company_identity_verified, representative_authorized, consent_version_verified, website_scope_verified, withdrawal_checked))
        decision = "APPROVED" if approve and not conflict and checks and submission["research_interest"] == "YES" and submission["research_authorization"] == "YES" else "REJECTED" if not approve else "BLOCKED"
        review_id = "review-" + _hash(submission_id + reviewer + decision)[:20]
        db.execute("INSERT OR REPLACE INTO consent_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (review_id, submission_id, record["profile_id"], decision, reviewer, int(company_identity_verified), int(representative_authorized), int(consent_version_verified), int(website_scope_verified), int(withdrawal_checked), notes, now()))
        if decision == "APPROVED":
            db.execute("UPDATE questionnaire_submissions SET consent_status='CONSENTED',review_status='APPROVED',company_identity_confirmed=1,website_analysis_allowed=1,identity_review_status='VERIFIED' WHERE submission_id=?", (submission_id,))
            db.execute("UPDATE research_records SET consent_status='CONSENTED',promotion_state='RESEARCH_PARTICIPANT',geo_research_status='HANDOFF_PENDING',geo_integration_status='HANDOFF_PENDING',geo_integration_error=NULL,updated_at=? WHERE profile_id=?", (now(), record["profile_id"]))
        else:
            status = "DECLINED" if decision == "REJECTED" else "NEEDS_REVIEW"
            db.execute("UPDATE questionnaire_submissions SET review_status=? WHERE submission_id=?", (decision, submission_id))
            db.execute("UPDATE research_records SET consent_status=?,promotion_state=CASE WHEN ? THEN 'BLOCKED' ELSE promotion_state END,geo_research_status=?,geo_integration_status=?,updated_at=? WHERE profile_id=?", (status, int(decision == "REJECTED"), "BLOCKED" if decision == "REJECTED" else "PENDING_REVIEW", "BLOCKED" if decision == "REJECTED" else "PENDING_REVIEW", now(), record["profile_id"]))
        db.commit()
    if decision != "APPROVED":
        return {"status": decision, "submission_id": submission_id, "checks_passed": checks}
    return process_geo_handoff(submission_id, path=path, bridge=bridge)
