import sqlite3

import pytest

from outreach_agent.google_forms_integration import INVITATION_REFERENCE_ENTRY, parse_sheet_row, prefilled_form_url, sync_sheet_rows
from outreach_agent.research_lifecycle import create_questionnaire_invitation, get_invitation_reference, ingest_google_form_response, register_profile


def _profile():
    return {
        "schema_version": "geo-company-profile/v1", "profile_id": "forms-company", "profile_type": "startup_company",
        "identity": {"name": "Forms Co", "website": "https://forms.example", "verification_status": "VERIFIED", "verification_evidence": [{"url": "https://forms.example"}]},
        "startup": {"industry": "software", "location": "Austin, TX"}, "qualification": {"eligibility": "ELIGIBLE"},
        "provenance": {"source_name": "test", "source_batch": "us"}, "contact_channels": [{"channel_type": "GENERAL_BUSINESS_EMAIL", "email": "owner@forms.example"}],
    }


def test_prefilled_url_uses_verified_entry_id():
    url = prefilled_form_url("GEO-ABC")
    assert INVITATION_REFERENCE_ENTRY in url
    assert "GEO-ABC" in url


def test_six_question_mapping_preserves_representative_fields():
    row = {
        "Timestamp": "2026-10-22T18:00:00Z",
        "Invitation Reference": "GEO-ABC",
        "Is your company interested in participating in this research?": "Yes",
        "Does your company authorize our research team to conduct the research described above?": "No",
        "Full Name of the Authorized Company Representative": "Test Representative",
        "Job Title of the Authorized Company Representative": "Founder",
        "Business Email Address of the Authorized Company Representative": "rep@example.test",
    }
    parsed = parse_sheet_row(row)
    assert parsed.research_interest == "YES"
    assert parsed.research_authorization == "NO"
    assert parsed.representative_name == "Test Representative"
    assert parsed.representative_role == "Founder"
    assert parsed.contact_email == "rep@example.test"


def test_legacy_four_question_rows_remain_review_only():
    parsed = parse_sheet_row({"Invitation Reference": "GEO-OLD", "Your Name and Role": "Owner / Founder", "Business Email Address": "owner@example.test", "Research Participation & Consent": "Yes"})
    assert parsed.research_interest == "YES"
    assert parsed.research_authorization == "NEEDS_REVIEW"
    assert parsed.representative_name == "Owner"
    assert parsed.representative_role == "Founder"


def test_google_sheet_response_is_idempotent_and_needs_review(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(_profile(), db)
    invitation_id, _ = create_questionnaire_invitation("forms-company", secret="secret", path=db)
    reference = get_invitation_reference(invitation_id, path=db)
    row = {"Timestamp": "2026-10-22T18:00:00Z", "Invitation Reference": reference, "Your Name and Role": "Owner / Founder", "Business Email Address": "owner@forms.example", "Research Participation & Consent": "Yes"}
    assert ingest_google_form_response(row, path=db) == "NEEDS_REVIEW"
    assert ingest_google_form_response(row, path=db) == "DUPLICATE"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select consent_status from research_records where profile_id='forms-company'").fetchone()[0] == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    ("interest", "authorization", "expected"),
    [("Yes", "Yes", "NEEDS_REVIEW"), ("Yes", "No", "INTERESTED_NOT_AUTHORIZED"), ("No", "No", "DECLINED"), ("No", "Yes", "NEEDS_REVIEW")],
)
def test_interest_and_authorization_decision_matrix(tmp_path, interest, authorization, expected):
    db = tmp_path / f"registry-{interest}-{authorization}.db"
    register_profile(_profile(), db)
    invitation_id, _ = create_questionnaire_invitation("forms-company", secret="secret", path=db)
    reference = get_invitation_reference(invitation_id, path=db)
    row = {
        "Timestamp": "2026-10-22T18:00:00Z", "Invitation Reference": reference,
        "Is your company interested in participating in this research?": interest,
        "Does your company authorize our research team to conduct the research described above?": authorization,
        "Full Name of the Authorized Company Representative": "Owner",
        "Job Title of the Authorized Company Representative": "Founder",
        "Business Email Address of the Authorized Company Representative": "owner@forms.example",
    }
    assert ingest_google_form_response(row, path=db) == expected
    with sqlite3.connect(db) as conn:
        submission = conn.execute("select research_interest,research_authorization,representative_full_name,representative_job_title,consent_status,review_status from questionnaire_submissions").fetchone()
    assert submission == (interest.upper(), authorization.upper(), "Owner", "Founder", expected, "NEEDS_REVIEW")


def test_representative_email_difference_is_retained_and_reviewed(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(_profile(), db)
    invitation_id, _ = create_questionnaire_invitation("forms-company", secret="secret", path=db)
    reference = get_invitation_reference(invitation_id, path=db)
    row = {"Invitation Reference": reference, "Is your company interested in participating in this research?": "Yes", "Does your company authorize our research team to conduct the research described above?": "Yes", "Full Name of the Authorized Company Representative": "Authorized Person", "Job Title of the Authorized Company Representative": "CEO", "Business Email Address of the Authorized Company Representative": "different@forms.example"}
    assert ingest_google_form_response(row, path=db) == "NEEDS_REVIEW"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select contact_email,identity_review_status from questionnaire_submissions").fetchone() == ("different@forms.example", "REPRESENTATIVE_EMAIL_DIFFERS_FROM_INVITATION")


def test_distinct_second_response_is_quarantined(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(_profile(), db)
    invitation_id, _ = create_questionnaire_invitation("forms-company", secret="secret", path=db)
    reference = get_invitation_reference(invitation_id, path=db)
    base = {
        "Invitation Reference": reference,
        "Is your company interested in participating in this research?": "Yes",
        "Does your company authorize our research team to conduct the research described above?": "Yes",
        "Full Name of the Authorized Company Representative": "Owner",
        "Job Title of the Authorized Company Representative": "Founder",
    }
    first = {**base, "Timestamp": "2026-10-22T18:00:00Z", "Business Email Address of the Authorized Company Representative": "owner@forms.example"}
    second = {**base, "Timestamp": "2026-10-22T18:05:00Z", "Business Email Address of the Authorized Company Representative": "other@forms.example"}
    assert ingest_google_form_response(first, path=db) == "NEEDS_REVIEW"
    assert ingest_google_form_response(second, path=db) == "NEEDS_REVIEW"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from questionnaire_submissions").fetchone()[0] == 2
        assert conn.execute("select count(*) from questionnaire_conflicts where status='QUARANTINED'").fetchone()[0] == 1


def test_unknown_reference_and_mismatch_are_not_consent(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(_profile(), db)
    invitation_id, _ = create_questionnaire_invitation("forms-company", secret="secret", path=db)
    reference = get_invitation_reference(invitation_id, path=db)
    unknown = {"Invitation Reference": "GEO-TAMPERED", "Your Name and Role": "Owner", "Business Email Address": "owner@forms.example", "Research Participation & Consent": "Yes"}
    assert ingest_google_form_response(unknown, path=db) == "UNKNOWN_REFERENCE"
    mismatch = {"Invitation Reference": reference, "Your Name and Role": "Owner", "Business Email Address": "other@example.com", "Research Participation & Consent": "Yes"}
    assert ingest_google_form_response(mismatch, path=db) == "NEEDS_REVIEW"


def test_sheet_row_batch_adapter_counts_results():
    statuses = iter(["RECEIVED", "DUPLICATE", "NEEDS_REVIEW"])
    rows = [{"Invitation Reference": "a"}, {"Invitation Reference": "b"}, {"Invitation Reference": "c"}]
    result = sync_sheet_rows(rows, process=lambda _: next(statuses))
    assert result == {"seen": 3, "processed": 1, "duplicates": 1, "needs_review": 1}
