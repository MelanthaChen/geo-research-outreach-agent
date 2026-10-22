import sqlite3

from outreach_agent.full_screening import deterministic_gate, plan_full_run, run_full_job
from outreach_agent.research_lifecycle import init_registry


def _db(tmp_path):
    path = tmp_path / "registry.db"
    init_registry(path)
    with sqlite3.connect(path) as db:
        values = [
            ("company-1", "business_company", "VERIFIED_US", "VERIFIED", "ELIGIBLE", "SIZE_UNKNOWN", "[{\"channel_type\":\"GENERAL_BUSINESS_EMAIL\",\"email\":\"owner@example.com\",\"source_url\":\"https://example.com/contact\"}]"),
            ("hotel-1", "hotel_property", "VERIFIED_US", "VERIFIED", "ELIGIBLE", "SIZE_UNKNOWN", "[{\"channel_type\":\"GENERAL_BUSINESS_EMAIL\",\"email\":\"desk@hotel.example\",\"source_url\":\"https://hotel.example/contact\"}]"),
            ("foreign-1", "business_company", "VERIFIED_NON_US", "VERIFIED", "ELIGIBLE", "KNOWN", "[]"),
            ("test-1", "owner_controlled_test", "COUNTRY_UNKNOWN", "OWNER_CONTROLLED_TEST", "OWNER_CONTROLLED_TEST", "SIZE_UNKNOWN", "[]"),
        ]
        for pid, ptype, geo, identity, qual, size, channels in values:
            db.execute("""INSERT INTO research_records(profile_id,profile_version,profile_type,name,official_website,industry,location,size_status,size_evidence_json,verification_evidence_json,provenance_json,contact_channels_json,cohort_membership_json,identity_verification_status,geographic_classification,geographic_evidence_json,contact_verification_status,screening_status,recruitment_priority,outreach_ready_status,qualification_status,recruitment_status,questionnaire_status,consent_status,geo_research_status,monitoring_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (pid,"v1",ptype,pid,"https://example.com","test","US","","[]","[]","[]",channels,"{}",identity,geo,"{}","NOT_VERIFIED","NOT_SCREENED","NEEDS_REVIEW","NOT_READY",qual,"NOT_INVITED","NOT_INVITED","NOT_ASKED","NOT_ELIGIBLE","NOT_STARTED","now","now"))
    return path


def test_deterministic_gates_treat_company_size_as_soft_and_allow_hotel(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        company = db.execute("select * from research_records where profile_id='company-1'").fetchone()
        hotel = db.execute("select * from research_records where profile_id='hotel-1'").fetchone()
        foreign = db.execute("select * from research_records where profile_id='foreign-1'").fetchone()
        owner = db.execute("select * from research_records where profile_id='test-1'").fetchone()
    assert deterministic_gate(company)[0] == "ELIGIBLE_FOR_AI_SCREENING"
    assert deterministic_gate(hotel)[0] == "ELIGIBLE_FOR_AI_SCREENING"
    assert "GEOGRAPHY_VERIFIED_NON_US" in deterministic_gate(foreign)[1]
    assert deterministic_gate(owner)[0] == "EXCLUDED_OWNER_CONTROLLED_TEST"


def test_full_run_is_idempotent_and_pauses_without_paid_ai(tmp_path):
    path = _db(tmp_path)
    first = plan_full_run(path)
    second = plan_full_run(path)
    assert first == second
    result = run_full_job(first, path)
    assert result["status"] == "PAUSED_AT_AI_BOUNDARY"
    assert result["paid_calls"] == 0
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from full_screening_queue where job_id=?", (first,)).fetchone()[0] == 4
        assert db.execute("select status from full_screening_queue where profile_id='hotel-1'").fetchone()[0] == "BLOCKED_AI_PROVIDER"
