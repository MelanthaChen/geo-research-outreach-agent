import sqlite3

import pytest

from outreach_agent.research_lifecycle import (
    CONSENT_VERSION,
    approve_email,
    classify_reply,
    create_email_draft,
    create_geo_handoff,
    create_questionnaire_invitation,
    init_registry,
    ingest_candidate_stores,
    list_records,
    record_simulated_send,
    suppress_recipient,
    register_profile,
    save_preliminary_screening,
    submit_questionnaire,
    questionnaire_url,
)


def profile(profile_id="company-1", profile_type="business_company"):
    return {"schema_version": "geo-company-profile/v1", "profile_id": profile_id, "profile_type": profile_type, "identity": {"name": "Example Co", "website": "https://example.test", "verification_evidence": [{"url": "https://example.test/about"}]}, "startup": {"location": "Austin, TX"}, "qualification": {"eligibility": "ELIGIBLE"}}


def test_registry_excludes_owner_fixture_and_preserves_company_identity(tmp_path):
    db = tmp_path / "registry.db"
    company = register_profile(profile(), db)
    test_site = register_profile(profile("test-1", "owner_controlled_test"), db)
    assert company["name"] == "Example Co"
    assert test_site["recruitment_status"] == "EXCLUDED"
    assert test_site["geo_research_status"] == "NOT_ELIGIBLE"
    row = sqlite3.connect(db).execute("SELECT profile_type FROM research_records WHERE profile_id='test-1'").fetchone()
    assert row[0] == "owner_controlled_test"


def test_screening_is_provenanced_and_rejects_unvalidated_uplift(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    screening = save_preliminary_screening("company-1", [{"title": "Clarify service entities", "claim": "potential to improve AI search visibility"}], [{"url": "https://example.test/about", "excerpt": "Observed text"}], path=db, provider="fixture", model="none", prompt_version="screen-v1")
    assert screening.startswith("screening-")
    with pytest.raises(ValueError):
        save_preliminary_screening("company-1", [{"title": "bad", "claim": "20% uplift"}], [{}], path=db)


def test_non_us_profile_is_blocked_from_screening_and_email(tmp_path):
    db = tmp_path / "registry.db"
    foreign = profile("foreign-1")
    foreign["startup"]["location"] = "London, United Kingdom"
    register_profile(foreign, db)
    with pytest.raises(PermissionError, match="VERIFIED_US"):
        save_preliminary_screening("foreign-1", [{"title": "Observe FAQ gaps"}], [{"url": "https://example.test"}], path=db)
    with pytest.raises(PermissionError, match="VERIFIED_US"):
        create_email_draft("foreign-1", "owner@example.test", "Invitation", path=db)


def test_questionnaire_token_consent_duplicate_and_handoff_gate(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, token = create_questionnaire_invitation("company-1", secret="test-secret", path=db)
    response = {"company_identity_confirmed": True, "respondent_name": "Owner", "respondent_role": "Founder", "contact_email": "owner@example.test", "agree_research": True, "website_analysis_allowed": True, "recommendations_requested": True, "ongoing_measurement_allowed": False, "site_modification_authorized": False, "consent_wording_version": CONSENT_VERSION}
    submission = submit_questionnaire(token, secret="test-secret", response=response, path=db)
    assert submit_questionnaire(token, secret="test-secret", response=response, path=db) == submission
    assert create_geo_handoff("company-1", submission, scope={"analysis": "website-only"}, path=db).startswith("handoff-")
    assert create_geo_handoff("company-1", submission, scope={"analysis": "website-only"}, path=db).startswith("handoff-")


def test_no_geo_handoff_before_consent_and_owner_fixture_blocked(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    with pytest.raises(PermissionError):
        create_geo_handoff("company-1", "missing", scope={}, path=db)
    register_profile(profile("test-1", "owner_controlled_test"), db)
    invitation_id, token = create_questionnaire_invitation("test-1", secret="s", path=db)
    submission = submit_questionnaire(token, secret="s", response={"company_identity_confirmed": True, "agree_research": True, "website_analysis_allowed": True}, path=db)
    with pytest.raises(PermissionError):
        create_geo_handoff("test-1", submission, scope={}, path=db)


def test_reply_classification_is_advisory_and_email_requires_approval(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    draft = create_email_draft("company-1", "owner@example.test", "A research invitation", path=db)
    with pytest.raises(PermissionError):
        record_simulated_send(draft, path=db)
    approve_email(draft, "reviewer", path=db)
    event = record_simulated_send(draft, path=db)
    assert record_simulated_send(draft, path=db) == event
    assert event.startswith("email-event-")
    assert classify_reply("I am interested, but what does this involve?") == "QUESTION"
    assert classify_reply("Please unsubscribe") == "UNSUBSCRIBE"
    assert classify_reply("I consent to the research") == "EXPLICIT_CONSENT"


def test_delivery_preview_suppression_and_expiry_guards(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    draft = create_email_draft("company-1", "owner@example.test", "Invitation", path=db)
    approve_email(draft, "reviewer", path=db)
    with pytest.raises(ValueError, match="preview hash"):
        record_simulated_send(draft, expected_preview_hash="stale", path=db)
    suppress_recipient("owner@example.test", path=db)
    with pytest.raises(PermissionError, match="suppressed"):
        record_simulated_send(draft, path=db)
    _, expired_token = create_questionnaire_invitation("company-1", secret="s", path=db, expires_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="expired"):
        submit_questionnaire(expired_token, secret="s", response={}, path=db)


def test_conflicting_questionnaire_responses_require_review(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    _, token = create_questionnaire_invitation("company-1", secret="s", path=db)
    first = {"company_identity_confirmed": True, "agree_research": True, "website_analysis_allowed": True}
    submit_questionnaire(token, secret="s", response=first, path=db)
    second = {"company_identity_confirmed": True, "agree_research": False, "website_analysis_allowed": False}
    submit_questionnaire(token, secret="s", response=second, path=db)
    row = sqlite3.connect(db).execute("SELECT consent_status FROM research_records WHERE profile_id='company-1'").fetchone()
    assert row[0] == "NEEDS_REVIEW"


def test_questionnaire_url_requires_public_base_outside_local(monkeypatch):
    monkeypatch.setenv("OUTREACH_ENV", "production")
    monkeypatch.delenv("RESEARCH_QUESTIONNAIRE_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        questionnaire_url("opaque-token")
    monkeypatch.setenv("RESEARCH_QUESTIONNAIRE_BASE_URL", "https://research.example.test")
    assert questionnaire_url("opaque-token") == "https://research.example.test/questionnaire?token=opaque-token"


def test_real_candidate_store_ingestion_is_incremental_and_cohort_separated(tmp_path):
    business = tmp_path / "business.db"
    hotel = tmp_path / "hotel.db"
    registry = tmp_path / "registry.db"
    with sqlite3.connect(business) as db:
        db.execute("CREATE TABLE candidates(candidate_id TEXT PRIMARY KEY,company_name TEXT,website TEXT,industry TEXT,location TEXT,source_name TEXT,source_url TEXT,source_record_id TEXT,source_license_or_permission TEXT,discovery_timestamp TEXT,verification_status TEXT,geo_eligibility_status TEXT,geo_opportunity_status TEXT,contact_status TEXT,human_review_status TEXT,outreach_ready_status TEXT,verification_reason TEXT,evidence_json TEXT,last_inspected_at TEXT)")
        db.execute("CREATE TABLE phase6f_contact_channels(candidate_id TEXT,channel_type TEXT,email TEXT,channel_url TEXT,source_url TEXT,evidence TEXT,purpose TEXT,review_status TEXT)")
        db.execute("CREATE TABLE phase6e_screening(candidate_id TEXT,screen_status TEXT)")
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("company-source-1","Verified Co","https://verified.test","Services","Boston","source","https://source.test/1","source-1","license","2026-01-01","VERIFIED","ELIGIBLE","HIGH","CONTACT_FOUND","PENDING","NOT_READY","first-party","{}","2026-01-02"))
        db.execute("INSERT INTO phase6f_contact_channels VALUES (?,?,?,?,?,?,?,?)", ("company-source-1","GENERAL_BUSINESS_EMAIL","owner@verified.test","","https://verified.test/contact","owner email","research","PENDING"))
    with sqlite3.connect(hotel) as db:
        db.execute("CREATE TABLE candidates(candidate_id TEXT PRIMARY KEY,company_name TEXT,website TEXT,industry TEXT,location TEXT,source_name TEXT,source_url TEXT,source_record_id TEXT,source_license_or_permission TEXT,discovery_timestamp TEXT,verification_status TEXT,geo_eligibility_status TEXT,geo_opportunity_status TEXT,contact_status TEXT,human_review_status TEXT,outreach_ready_status TEXT,verification_reason TEXT,evidence_json TEXT,last_inspected_at TEXT)")
        db.execute("CREATE TABLE hotel_contact_channels(candidate_id TEXT,channel_type TEXT,email TEXT,channel_url TEXT,source_url TEXT,evidence TEXT,purpose TEXT,review_status TEXT,discovered_at TEXT)")
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("hotel-source-1","Property Hotel","https://hotel.test","hotel","Albany","OSM","https://osm.test/1","node/1","ODbL","2026-01-01","VERIFIED","ELIGIBLE","MEDIUM","CONTACT_FOUND","PENDING","NOT_READY","first-party","{}","2026-01-02"))
        db.execute("INSERT INTO hotel_contact_channels VALUES (?,?,?,?,?,?,?,?,?)", ("hotel-source-1","CONTACT_FORM","","https://hotel.test/contact","https://hotel.test/contact","Message form","research","PENDING","2026-01-02"))
    assert ingest_candidate_stores(business_db=business, hotel_db=hotel, registry_db=registry) == {"business_seen": 1, "hotel_seen": 1, "inserted": 2, "updated": 0}
    with sqlite3.connect(registry) as db:
        rows = db.execute("SELECT profile_id,profile_type,identity_verification_status,contact_verification_status,cohort_membership_json FROM research_records ORDER BY profile_id").fetchall()
        db.execute("UPDATE research_records SET consent_status='CONSENTED' WHERE profile_id='company-source-1'")
    assert rows[0][0] == "company-source-1"
    assert rows[0][1] == "business_company"
    assert rows[0][2] == "VERIFIED"
    assert rows[0][3] == "CONTACT_EVIDENCE_PRESENT"
    assert rows[1][1] == "hotel_property"
    assert '"hotel": true' in rows[1][4]
    assert ingest_candidate_stores(business_db=business, hotel_db=hotel, registry_db=registry)["inserted"] == 0
    with sqlite3.connect(registry) as db:
        assert db.execute("SELECT consent_status FROM research_records WHERE profile_id='company-source-1'").fetchone()[0] == "CONSENTED"


def test_research_queue_pagination_and_status_filters(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile("company-a"), db)
    register_profile({**profile("company-b"), "identity": {"name": "Other Co", "website": "https://other.test", "verification_status": "DISCOVERED_UNVERIFIED"}}, db)
    page = list_records(db, profile_type="business_company", limit=1, offset=1)
    assert page["total"] == 2
    assert len(page["records"]) == 1
    filtered = list_records(db, identity_status="DISCOVERED_UNVERIFIED")
    assert filtered["total"] == 1
    assert filtered["records"][0]["profile_id"] == "company-b"


def test_operational_queue_can_include_owner_fixture_without_changing_formal_filter(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile("company-a"), db)
    register_profile(profile("owner-test", "owner_controlled_test"), db)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE research_records SET promotion_state='PROSPECT' WHERE profile_id='company-a'")
        connection.commit()
    formal = list_records(db, promotion_state="OPERATIONAL")
    visible = list_records(db, promotion_state="OPERATIONAL_WITH_TEST")
    assert formal["total"] == 1
    assert visible["total"] == 2
    assert {row["profile_id"] for row in visible["records"]} == {"company-a", "owner-test"}

