import sqlite3

import pytest

from outreach_agent.geo_engine_handoff import process_geo_handoff, review_questionnaire_submission
from outreach_agent.research_lifecycle import create_email_draft, create_questionnaire_invitation, get_invitation_reference, ingest_google_form_response, register_profile


def owner_profile():
    return {
        "schema_version": "geo-company-profile/v1",
        "profile_id": "geoairesume-owner-test-v1",
        "profile_type": "owner_controlled_test",
        "identity": {"name": "GeoAIResume", "website": "https://geo-ai-resume.vercel.app", "verification_status": "VERIFIED"},
        "startup": {"location": "Boston, MA", "industry": "software"},
        "qualification": {"eligibility": "OWNER_CONTROLLED_TEST"},
    }


class FakeBridge:
    def __init__(self):
        self.calls = []

    def import_profile(self, profile):
        self.calls.append(profile)
        return {"property_id": 77, "source_profile_id": profile["profile_id"]}


def setup_response(tmp_path):
    path = tmp_path / "registry.db"
    register_profile(owner_profile(), path)
    invitation_id, _ = create_questionnaire_invitation("geoairesume-owner-test-v1", secret="secret", path=path)
    reference = get_invitation_reference(invitation_id, path=path)
    draft_id = create_email_draft("geoairesume-owner-test-v1", "owner@geoairesume.test", "test invitation", path=path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft_id))
    row = {"Timestamp": "2026-10-22T18:00:00Z", "Invitation Reference": reference, "Is your company interested in participating in this research?": "Yes", "Does your company authorize our research team to conduct the research described above?": "Yes", "Full Name of the Authorized Company Representative": "Owner Test", "Job Title of the Authorized Company Representative": "Founder", "Business Email Address of the Authorized Company Representative": "owner@geoairesume.test"}
    assert ingest_google_form_response(row, path=path) == "NEEDS_REVIEW"
    submission_id = sqlite3.connect(path).execute("SELECT submission_id FROM questionnaire_submissions").fetchone()[0]
    return path, invitation_id, submission_id


def test_explicit_review_automatically_registers_property_and_is_idempotent(tmp_path):
    path, invitation_id, submission_id = setup_response(tmp_path)
    bridge = FakeBridge()
    result = review_questionnaire_submission(submission_id, reviewer="owner-reviewer", company_identity_verified=True, representative_authorized=True, consent_version_verified=True, website_scope_verified=True, withdrawal_checked=True, approve=True, path=path, bridge=bridge)
    assert result["status"] == "HANDED_OFF"
    assert result["property_id"] == 77
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["consent"]["status"] == "VERIFIED"
    again = process_geo_handoff(submission_id, path=path, bridge=bridge)
    assert again["idempotent"] is True
    assert len(bridge.calls) == 1
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT consent_status,geo_property_id,geo_integration_status,geo_research_status FROM research_records").fetchone()
        assert row == ("CONSENTED", 77, "HANDED_OFF", "HANDED_OFF")
        assert db.execute("SELECT COUNT(*) FROM geo_handoff_attempts").fetchone()[0] == 1


def test_distinct_sheet_responses_are_quarantined(tmp_path):
    path, invitation_id, submission_id = setup_response(tmp_path)
    reference = get_invitation_reference(invitation_id, path=path)
    conflicting = {"Timestamp": "2026-10-22T18:01:00Z", "Invitation Reference": reference, "Is your company interested in participating in this research?": "No", "Does your company authorize our research team to conduct the research described above?": "No", "Full Name of the Authorized Company Representative": "Different Test", "Job Title of the Authorized Company Representative": "Owner", "Business Email Address of the Authorized Company Representative": "owner@geoairesume.test"}
    assert ingest_google_form_response(conflicting, path=path) == "NEEDS_REVIEW"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM questionnaire_conflicts WHERE status='QUARANTINED'").fetchone()[0] == 1
        assert db.execute("SELECT geo_integration_status FROM research_records").fetchone()[0] == "BLOCKED_CONFLICT"
    with pytest.raises(PermissionError, match="quarantined"):
        process_geo_handoff(submission_id, path=path, bridge=FakeBridge())


def test_review_never_approves_without_all_checks(tmp_path):
    path, _, submission_id = setup_response(tmp_path)
    result = review_questionnaire_submission(submission_id, reviewer="reviewer", company_identity_verified=True, representative_authorized=False, consent_version_verified=True, website_scope_verified=True, withdrawal_checked=True, approve=True, path=path, bridge=FakeBridge())
    assert result["status"] == "BLOCKED"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT consent_status FROM research_records").fetchone()[0] == "NEEDS_REVIEW"


def test_representative_email_can_differ_from_invitation_recipient_when_reviewed(tmp_path):
    path = tmp_path / "registry.db"
    register_profile(owner_profile(), path)
    invitation_id, _ = create_questionnaire_invitation("geoairesume-owner-test-v1", secret="secret", path=path)
    reference = get_invitation_reference(invitation_id, path=path)
    draft_id = create_email_draft("geoairesume-owner-test-v1", "invited@example.test", "test invitation", path=path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft_id))
    row = {
        "Timestamp": "2026-10-22T18:00:00Z",
        "Invitation Reference": reference,
        "Is your company interested in participating in this research?": "Yes",
        "Does your company authorize our research team to conduct the research described above?": "Yes",
        "Full Name of the Authorized Company Representative": "Authorized Representative",
        "Job Title of the Authorized Company Representative": "Founder",
        "Business Email Address of the Authorized Company Representative": "representative@example.test",
    }
    assert ingest_google_form_response(row, path=path) == "NEEDS_REVIEW"
    submission_id = sqlite3.connect(path).execute("SELECT submission_id FROM questionnaire_submissions").fetchone()[0]
    result = review_questionnaire_submission(
        submission_id,
        reviewer="owner-reviewer",
        company_identity_verified=True,
        representative_authorized=True,
        consent_version_verified=True,
        website_scope_verified=True,
        withdrawal_checked=True,
        approve=True,
        path=path,
        bridge=FakeBridge(),
    )
    assert result["status"] == "HANDED_OFF"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT identity_review_status FROM questionnaire_submissions").fetchone()[0] == "VERIFIED"
