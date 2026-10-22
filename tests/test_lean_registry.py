import json
import sqlite3

from outreach_agent.lean_registry import promote_sources, review_contact
from outreach_agent.research_lifecycle import create_geo_handoff, create_questionnaire_invitation, register_profile, submit_questionnaire


def _source_db(path, *, hotel=False):
    contact_table = "hotel_contact_channels" if hotel else "phase6f_contact_channels"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE candidates(
            candidate_id TEXT PRIMARY KEY, company_name TEXT, website TEXT, canonical_domain TEXT,
            industry TEXT, location TEXT, address TEXT, source_name TEXT, source_url TEXT,
            source_record_id TEXT, source_license_or_permission TEXT, discovery_timestamp TEXT,
            verification_status TEXT, geo_eligibility_status TEXT, geo_opportunity_status TEXT,
            contact_status TEXT, human_review_status TEXT, outreach_ready_status TEXT,
            verification_reason TEXT, evidence_json TEXT, last_inspected_at TEXT, source_batch TEXT)""")
        db.execute(f"""CREATE TABLE {contact_table}(
            candidate_id TEXT, channel_type TEXT, email TEXT, channel_url TEXT,
            source_url TEXT, evidence TEXT, purpose TEXT, review_status TEXT, discovered_at TEXT)""")
        rows = [
            ("eligible-email", "Eligible Email", "https://email.test", "email.test", "Services", "Boston, MA", "", "SRC", "https://src/1", "1", "license", "2026", "VERIFIED", "ELIGIBLE", "HIGH", "CONTACT_FOUND", "PENDING", "NOT_READY", "verified", "{}", "2026", "batch"),
            ("eligible-form", "Eligible Form", "https://form.test", "form.test", "Services", "Austin, TX", "", "SRC", "https://src/2", "2", "license", "2026", "VERIFIED", "ELIGIBLE", "MEDIUM", "CONTACT_FOUND", "PENDING", "NOT_READY", "verified", "{}", "2026", "batch"),
            ("eligible-both", "Eligible Both", "https://both.test", "both.test", "Services", "New York, NY", "", "SRC", "https://src/3", "3", "license", "2026", "VERIFIED", "ELIGIBLE", "HIGH", "CONTACT_FOUND", "PENDING", "NOT_READY", "verified", "{}", "2026", "batch"),
            ("unknown", "Unknown", "https://unknown.test", "unknown.test", "Services", "", "", "SRC", "https://src/4", "4", "license", "2026", "VERIFIED", "ELIGIBLE", "HIGH", "CONTACT_FOUND", "PENDING", "NOT_READY", "verified", "{}", "2026", "batch"),
            ("foreign", "Foreign", "https://foreign.test", "foreign.test", "Services", "London, United Kingdom", "", "SRC", "https://src/5", "5", "license", "2026", "VERIFIED", "ELIGIBLE", "HIGH", "CONTACT_FOUND", "PENDING", "NOT_READY", "verified", "{}", "2026", "batch"),
            ("unverified", "Unverified", "https://unverified.test", "unverified.test", "Services", "Boston, MA", "", "SRC", "https://src/6", "6", "license", "2026", "DISCOVERED_UNVERIFIED", "ELIGIBLE", "HIGH", "CONTACT_FOUND", "PENDING", "NOT_READY", "unknown", "{}", "2026", "batch"),
        ]
        if hotel:
            rows = [tuple((f"hotel-{row[0]}" if index == 0 else value) for index, value in enumerate(row)) for row in rows]
        db.executemany("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        channels = [
            ("eligible-email", "GENERAL_BUSINESS_EMAIL", "owner@email.test", "", "https://email.test/contact", "Observed public business email", "general", "PENDING", "2026"),
            ("eligible-form", "CONTACT_FORM", "", "https://form.test/contact", "https://form.test/contact", "Observed official form", "general", "PENDING", "2026"),
            ("eligible-both", "GENERAL_BUSINESS_EMAIL", "owner@both.test", "", "https://both.test/contact", "Observed email", "general", "PENDING", "2026"),
            ("eligible-both", "CONTACT_FORM", "", "https://both.test/contact", "https://both.test/contact", "Observed form", "general", "PENDING", "2026"),
            ("unknown", "GENERAL_BUSINESS_EMAIL", "owner@unknown.test", "", "https://unknown.test/contact", "Observed email", "general", "PENDING", "2026"),
            ("foreign", "CONTACT_FORM", "", "https://foreign.test/contact", "https://foreign.test/contact", "Observed form", "general", "PENDING", "2026"),
            ("unverified", "GENERAL_BUSINESS_EMAIL", "owner@unverified.test", "", "https://unverified.test/contact", "Observed email", "general", "PENDING", "2026"),
        ]
        if hotel:
            channels = [tuple((f"hotel-{row[0]}" if index == 0 else value) for index, value in enumerate(row)) for row in channels]
        db.executemany(f"INSERT INTO {contact_table} VALUES (?,?,?,?,?,?,?,?,?)", channels)


def test_lean_registry_promotes_actionable_prospects_without_verifying_contacts(tmp_path):
    business = tmp_path / "business.db"
    hotel = tmp_path / "hotel.db"
    target = tmp_path / "lean.db"
    _source_db(business)
    _source_db(hotel, hotel=True)
    report = promote_sources(business_db=business, hotel_db=hotel, target=target)
    # The fixture is intentionally duplicated as company and hotel source data.
    assert report["companies"]["qualified_actionable"] == 3
    assert report["companies"]["email_only"] == 1
    assert report["companies"]["form_only"] == 1
    assert report["companies"]["email_and_form"] == 1
    assert report["totals"]["outreach_ready"] == 0
    with sqlite3.connect(target) as db:
        rows = db.execute("SELECT outreach_ready_status,contact_verification_status FROM research_records ORDER BY profile_id").fetchall()
        assert len(rows) == 6
        assert set(rows) == {("NOT_READY", "CONTACT_REVIEW_PENDING")}
        assert db.execute("SELECT COUNT(*) FROM prospect_contacts WHERE email_verification_status='NOT_VERIFIED'").fetchone()[0] == 8
        assert "source_row_json" not in {row[1] for row in db.execute("PRAGMA table_info(research_records)")}


def test_lean_registry_is_idempotent_and_preserves_source(tmp_path):
    business = tmp_path / "business.db"
    hotel = tmp_path / "hotel.db"
    target = tmp_path / "lean.db"
    _source_db(business)
    _source_db(hotel, hotel=True)
    first = promote_sources(business_db=business, hotel_db=hotel, target=target)
    second = promote_sources(business_db=business, hotel_db=hotel, target=target)
    assert first["totals"] == second["totals"]
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM research_records").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM prospect_contacts").fetchone()[0] == 8
    with sqlite3.connect(business) as db:
        assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6


def test_contact_review_and_owner_fixture_use_the_same_canonical_registry(tmp_path):
    business = tmp_path / "business.db"
    hotel = tmp_path / "hotel.db"
    target = tmp_path / "lean.db"
    _source_db(business)
    _source_db(hotel, hotel=True)
    promote_sources(business_db=business, hotel_db=hotel, target=target)
    with sqlite3.connect(target) as db:
        contact_id = db.execute("SELECT contact_id FROM prospect_contacts WHERE prospect_id='eligible-email'").fetchone()[0]
    result = review_contact(prospect_id="eligible-email", contact_id=contact_id, decision="APPROVED", reviewer="reviewer", path=target)
    assert result["outreach_ready"] is True
    with sqlite3.connect(target) as db:
        row = db.execute("SELECT contact_verification_status,outreach_ready_status,promotion_state FROM research_records WHERE profile_id='eligible-email'").fetchone()
        assert row == ("CONTACT_VERIFIED", "READY", "OUTREACH_READY")

    owner = {
        "schema_version": "geo-company-profile/v1", "profile_id": "geoairesume-test",
        "profile_type": "owner_controlled_test",
        "identity": {"name": "GeoAIResume", "website": "https://geo-ai-resume.vercel.app"},
        "startup": {"location": "Boston, MA"}, "qualification": {"eligibility": "OWNER_CONTROLLED_TEST"},
    }
    register_profile(owner, target)
    invitation_id, token = create_questionnaire_invitation("geoairesume-test", secret="test", path=target)
    submission = submit_questionnaire(token, secret="test", response={"company_identity_confirmed": True, "agree_research": True, "website_analysis_allowed": True}, path=target)
    assert create_geo_handoff("geoairesume-test", submission, scope={"fixture_protocol": "OWNER_CONTROLLED_TEST_LOCAL_ONLY"}, path=target).startswith("handoff-")
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='owner_controlled_test'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type IN ('business_company','hotel_property')").fetchone()[0] == 6
