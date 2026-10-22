import json
from pathlib import Path

import pytest

from outreach_agent.integration import (
    CONTRACT_VERSION,
    OWNER_CONTROLLED_TEST,
    build_profile_import,
    classify_contact_channels,
    discover_public_contact,
    import_result_envelope,
    make_owner_controlled_test_registry_record,
)


def old_profile(profile_id="osm-hotel:node/7"):
    return {
        "schema_version": "geo-company-profile/v1",
        "profile_id": profile_id,
        "profile_type": "hotel_property",
        "identity": {"name": "Property Hotel", "website": "https://property.example"},
        "cohort_membership": {"hotel": True, "smb": False},
    }


def test_owner_controlled_test_is_excluded_from_cohorts_and_campaign_actions():
    registry = make_owner_controlled_test_registry_record()
    assert registry["record_classification"] == OWNER_CONTROLLED_TEST
    assert registry["formal_cohort_memberships"] == []
    assert "SEND_EMAIL" in registry["prohibited_actions"]
    assert "SUBMIT_FORM" in registry["prohibited_actions"]
    imported = build_profile_import({**old_profile("geoairesume-owner-test-v1"), "profile_type": "owner_controlled_test", "identity": {"name": "GeoAIResume", "website": "https://geo-ai-resume.vercel.app"}})
    assert imported["profile_type"] == "owner_controlled_test"
    assert imported["formal_cohort_memberships"] == []
    assert imported["exclusion_reason"] == "CONTROLLED_TEST_FIXTURE"


def test_hotel_property_identity_is_preserved():
    imported = build_profile_import(old_profile())
    assert imported["profile_type"] == "hotel_property"
    assert imported["source_profile_id"] == "osm-hotel:node/7"
    assert imported["profile_snapshot"]["identity"]["name"] == "Property Hotel"


def test_contact_channel_classification_and_form_evidence():
    assert classify_contact_channels([]) == "NO_CONTACT_CHANNEL"
    html = '<html><body>Owner: owner@example.test<form action="/contact"><textarea name="message"></textarea><button>Send</button></form></body></html>'
    result = discover_public_contact(source_url="https://geo-ai-resume.vercel.app/contact", html=html, captured_at="2026-10-22T12:00:00+00:00")
    assert result["channel_type"] == "EMAIL_AND_FORM"
    assert result["channels"][0]["source_url"].startswith("https://")
    assert result["channels"][0]["captured_at"] == "2026-10-22T12:00:00+00:00"
    page_only = discover_public_contact(source_url="https://geo-ai-resume.vercel.app/contact", html="<p>Contact us</p>")
    assert page_only["channel_type"] == "NO_CONTACT_CHANNEL"


def test_result_import_is_idempotent_and_preserves_missing_experiment(tmp_path):
    result = {
        "contract_version": CONTRACT_VERSION,
        "source_profile_id": "company-1",
        "source_profile_version": "geo-company-profile/v1",
        "property_id": 12,
        "audit": {"audit_id": 4, "status": "COMPLETED", "audit_url": "https://example.test", "page_refs": [], "captured_at": "2026-10-22T12:00:00+00:00"},
        "baseline": {"experiment_id": "baseline-1", "status": "PENDING", "results_present": False, "created_at": "2026-10-22T12:00:00+00:00"},
    }
    path = tmp_path / "results.jsonl"
    assert import_result_envelope(result, path)["added"] == 2
    assert import_result_envelope(result, path)["skipped"] == 2
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2
    assert all(row["source_profile_id"] == "company-1" for row in rows)


def test_result_import_rejects_conflicting_retry(tmp_path):
    result = {
        "contract_version": CONTRACT_VERSION,
        "source_profile_id": "company-1", "source_profile_version": "geo-company-profile/v1", "property_id": 12,
        "audit": {"record_id": "audit-fixed", "audit_id": 4, "status": "COMPLETED", "audit_url": "https://example.test", "page_refs": [], "captured_at": "2026-10-22T12:00:00+00:00"},
    }
    path = tmp_path / "results.jsonl"
    import_result_envelope(result, path)
    result["audit"]["status"] = "FAILED"
    with pytest.raises(ValueError, match="conflicting retry"):
        import_result_envelope(result, path)

