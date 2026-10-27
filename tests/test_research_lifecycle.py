from workflow_test_support import add_personalization
import sqlite3

import pytest

from outreach_agent.research_lifecycle import (
    CONSENT_VERSION,
    approve_email,
    classify_reply,
    ingest_eml_reply,
    create_email_draft,
    create_geo_handoff,
    create_questionnaire_invitation,
    create_post_form_confirmation_draft,
    init_registry,
    ingest_google_form_response,
    ingest_candidate_stores,
    list_records,
    record_simulated_send,
    suppress_recipient,
    register_profile,
    save_preliminary_screening,
    submit_questionnaire,
    questionnaire_url,
    create_manual_invitation_draft,
    reconcile_manual_send,
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


def test_manual_invitation_uses_test_override_without_mutating_source(tmp_path):
    db = tmp_path / "registry.db"
    register_profile({**profile("owner", "owner_controlled_test"), "identity": {"name": "GeoAIResume", "website": "https://geo-ai-resume.vercel.app", "verification_evidence": [{"url": "https://geo-ai-resume.vercel.app", "text": "Owner-controlled test website"}]}}, db)
    before = sqlite3.connect(db).execute("SELECT contact_channels_json,source_candidate_id FROM research_records WHERE profile_id='owner'").fetchone()
    add_personalization(db, "owner")
    result = create_manual_invitation_draft("owner", delivery_recipient="yuxuan.chen.1031@outlook.com", secret="secret", path=db, test_override=True)
    assert result["status"] == "DRAFTED"
    assert result["test_override"] is True
    assert result["intended_recipient"] is None
    assert result["invitation_reference"].startswith("GEO-")
    assert result["invitation_reference"] in result["body"]
    assert "geo-ai-resume.vercel.app" in result["body"]
    after = sqlite3.connect(db).execute("SELECT contact_channels_json,source_candidate_id FROM research_records WHERE profile_id='owner'").fetchone()
    assert after == before
    assert create_manual_invitation_draft("owner", delivery_recipient="yuxuan.chen.1031@outlook.com", secret="secret", path=db, test_override=True)["status"] == "EXISTING"


def test_manual_invitation_rejects_unallowlisted_test_recipient_and_requires_send_evidence(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile("owner", "owner_controlled_test"), db)
    with pytest.raises(PermissionError, match="allowlist"):
        create_manual_invitation_draft("owner", delivery_recipient="other@example.com", secret="secret", path=db, test_override=True)
    add_personalization(db, "owner")
    result = create_manual_invitation_draft("owner", delivery_recipient="yuxaun.chen.1031@outlook.com", secret="secret", path=db, test_override=True)
    with pytest.raises(PermissionError, match="approved"):
        reconcile_manual_send(result["draft_id"], provider="OUTLOOK_WORD_MAIL_MERGE", provider_message_id="m1", evidence_ref="sent-item-1", path=db)
    from outreach_agent.research_lifecycle import approve_email
    approve_email(result["draft_id"], "operator", path=db)
    with pytest.raises(ValueError, match="evidence"):
        reconcile_manual_send(result["draft_id"], provider="OUTLOOK_WORD_MAIL_MERGE", provider_message_id="m1", evidence_ref="", path=db)
    accepted = reconcile_manual_send(result["draft_id"], provider="OUTLOOK_WORD_MAIL_MERGE", provider_message_id="m1", evidence_ref="sent-item-1", path=db)
    assert accepted["status"] == "RECONCILED"
    assert reconcile_manual_send(result["draft_id"], provider="OUTLOOK_WORD_MAIL_MERGE", provider_message_id="m1", evidence_ref="sent-item-1", path=db)["status"] == "EXISTING"
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 1


def test_outlook_eml_reply_preserves_headers_and_does_not_grant_consent(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("company-1", secret="secret", path=db)
    from outreach_agent.research_lifecycle import get_invitation_reference
    reference = get_invitation_reference(invitation_id, path=db)
    draft_id = create_email_draft("company-1", "invitee@example.test", "Invitation", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft_id))
    eml = tmp_path / "reply.eml"
    eml.write_text(
        f"From: Representative <rep@example.test>\nTo: chen.yuxuan9@northeastern.edu\n"
        f"Subject: Re: GEO invitation {reference}\nDate: Thu, 23 Oct 2026 10:00:00 -0400\n"
        "Message-ID: <reply-123@example.test>\nIn-Reply-To: <invite-123@example.test>\n"
        "References: <invite-123@example.test>\nContent-Type: text/plain; charset=utf-8\n\n"
        "I am interested and would like to understand the study.\n",
        encoding="utf-8",
    )
    result = ingest_eml_reply(eml, path=db)
    assert result["correlation_status"] == "CORRELATED_BY_REFERENCE"
    assert result["message_id"] == "<reply-123@example.test>"
    assert result["in_reply_to"] == "<invite-123@example.test>"
    assert result["review_status"] == "NEEDS_REVIEW"
    assert result["consent_changed"] is False
    with sqlite3.connect(db) as conn:
        reply = conn.execute("SELECT sender,recipient,subject,message_id,raw_content,correlation_status FROM replies").fetchone()
        assert reply[0].startswith("Representative")
        assert reply[1] == "chen.yuxuan9@northeastern.edu"
        assert reply[3] == "<reply-123@example.test>"
        assert "I am interested" in reply[4]
        assert conn.execute("SELECT consent_status FROM research_records").fetchone()[0] == "NOT_ASKED"


def test_outlook_eml_reply_with_no_unique_match_requires_review(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    eml = tmp_path / "unmatched.eml"
    eml.write_text(
        "From: Unknown <unknown@example.test>\nTo: team@example.test\nSubject: Re: GEO\n"
        "Message-ID: <unknown@example.test>\nContent-Type: text/plain\n\nPlease tell me more.\n",
        encoding="utf-8",
    )
    result = ingest_eml_reply(eml, path=db)
    assert result["correlation_status"] == "UNMATCHED"
    assert result["profile_id"] is None
    assert result["consent_changed"] is False


def test_post_form_confirmation_uses_verified_official_email_not_other_roles(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("company-1", secret="secret", path=db)
    from outreach_agent.research_lifecycle import get_invitation_reference
    reference = get_invitation_reference(invitation_id, path=db)
    invitation_draft = create_email_draft("company-1", "invitation@example.test", "Invitation", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, invitation_draft))
        conn.execute("UPDATE research_records SET contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? WHERE profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"official@example.test","review_status":"APPROVED","source_url":"https://example.test/contact"}]',))
    row = {
        "Timestamp": "2026-10-23T12:00:00Z", "Invitation Reference": reference,
        "Is your company interested in participating in this research?": "Yes",
        "Does your company authorize our research team to conduct the research described above?": "Yes",
        "Full Name of the Authorized Company Representative": "Representative Name",
        "Job Title of the Authorized Company Representative": "CEO",
        "Business Email Address of the Authorized Company Representative": "representative@example.test",
    }
    assert ingest_google_form_response(row, path=db) == "NEEDS_REVIEW"
    submission_id = sqlite3.connect(db).execute("SELECT submission_id FROM questionnaire_submissions").fetchone()[0]
    result = create_post_form_confirmation_draft(submission_id, path=db)
    assert result["status"] == "DRAFT"
    assert result["official_company_email"] == "official@example.test"
    assert result["invitation_recipient_email"] == "invitation@example.test"
    assert result["representative_email"] == "representative@example.test"
    assert result["confirmation_policy"] == "AFFIRMATIVE_REPLY_REQUIRED"
    assert "official@example.test" in result["official_company_email"]
    with sqlite3.connect(db) as conn:
        draft = conn.execute("SELECT recipient,template_version,approval_status FROM email_drafts WHERE draft_id=?", (result["draft_id"],)).fetchone()
        assert draft == ("official@example.test", "post-form-confirmation-v1", "PENDING")
        assert conn.execute("SELECT confirmation_policy,status,review_status FROM confirmation_emails WHERE confirmation_id=?", (result["confirmation_id"],)).fetchone() == ("AFFIRMATIVE_REPLY_REQUIRED", "DRAFT", "PENDING")


def test_confirmation_policy_is_explicit_and_notice_only_is_not_inferred(tmp_path, monkeypatch):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("company-1", secret="secret", path=db)
    from outreach_agent.research_lifecycle import get_invitation_reference
    reference = get_invitation_reference(invitation_id, path=db)
    invitation_draft = create_email_draft("company-1", "invitation@example.test", "Invitation", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, invitation_draft))
        conn.execute("UPDATE research_records SET contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? WHERE profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"official@example.test","review_status":"VERIFIED"}]',))
    row = {
        "Invitation Reference": reference,
        "Is your company interested in participating in this research?": "Yes",
        "Does your company authorize our research team to conduct the research described above?": "Yes",
        "Full Name of the Authorized Company Representative": "Representative",
        "Job Title of the Authorized Company Representative": "CEO",
        "Business Email Address of the Authorized Company Representative": "representative@example.test",
    }
    assert ingest_google_form_response(row, path=db) == "NEEDS_REVIEW"
    submission_id = sqlite3.connect(db).execute("SELECT submission_id FROM questionnaire_submissions").fetchone()[0]
    monkeypatch.setenv("OUTREACH_CONFIRMATION_POLICY", "NOTICE_ONLY")
    result = create_post_form_confirmation_draft(submission_id, path=db)
    assert result["confirmation_policy"] == "NOTICE_ONLY"
    assert "formal confirmation notice" in result["body"]
    assert "not proof of authorization" in result["body"]


def test_invalid_confirmation_policy_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_CONFIRMATION_POLICY", "AUTO_APPROVE")
    with pytest.raises(ValueError, match="OUTREACH_CONFIRMATION_POLICY"):
        from outreach_agent.research_lifecycle import configured_confirmation_policy
        configured_confirmation_policy()


def test_post_form_confirmation_blocks_explicit_no_and_unverified_email(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("company-1", secret="secret", path=db)
    from outreach_agent.research_lifecycle import get_invitation_reference
    reference = get_invitation_reference(invitation_id, path=db)
    draft = create_email_draft("company-1", "invitation@example.test", "Invitation", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft))
    row = {"Timestamp": "2026-10-27T12:00:00Z", "Invitation Reference": reference, "Is your company interested in participating in this research?": "Yes", "Does your company authorize our research team to conduct the research described above?": "No", "Full Name of the Authorized Company Representative": "Representative", "Job Title of the Authorized Company Representative": "CEO", "Business Email Address of the Authorized Company Representative": "representative@example.test"}
    assert ingest_google_form_response(row, path=db) == "INTERESTED_NOT_AUTHORIZED"
    submission_id = sqlite3.connect(db).execute("SELECT submission_id FROM questionnaire_submissions").fetchone()[0]
    assert create_post_form_confirmation_draft(submission_id, path=db)["status"] == "BLOCKED_EXPLICIT_AUTHORIZATION_NO"


def test_confirmation_schema_migration_is_additive_and_preserves_existing_record(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE research_records SET name='Existing durable record' WHERE profile_id='company-1'")
    init_registry(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM research_records WHERE profile_id='company-1'").fetchone()[0] == "Existing durable record"
        submission_columns = {row[1] for row in conn.execute("PRAGMA table_info(questionnaire_submissions)")}
        assert {"invitation_recipient_email", "representative_email"} <= submission_columns
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='confirmation_emails'").fetchone() is not None


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

