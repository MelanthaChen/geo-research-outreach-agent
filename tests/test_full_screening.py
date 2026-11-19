import sqlite3
from types import SimpleNamespace

from outreach_agent.full_screening import deterministic_gate, plan_full_run, run_full_job
from outreach_agent.research_lifecycle import init_registry


def _db(tmp_path):
    path = tmp_path / "registry.db"
    init_registry(path)
    with sqlite3.connect(path) as db:
        values = [
            ("company-1", "business_company", "VERIFIED_US", "VERIFIED", "ELIGIBLE", "SIZE_UNKNOWN", "[{\"channel_type\":\"GENERAL_BUSINESS_EMAIL\",\"email\":\"owner@example.com\",\"source_url\":\"https://example.com/contact\",\"evidence\":\"Email owner@example.com for business inquiries\",\"review_status\":\"VERIFIED\"}]"),
            ("hotel-1", "hotel_property", "VERIFIED_US", "VERIFIED", "ELIGIBLE", "SIZE_UNKNOWN", "[{\"channel_type\":\"GENERAL_BUSINESS_EMAIL\",\"email\":\"desk@hotel.example\",\"source_url\":\"https://hotel.example/contact\",\"evidence\":\"Email desk@hotel.example for reservations and business inquiries\",\"review_status\":\"VERIFIED\"}]"),
            ("foreign-1", "business_company", "VERIFIED_NON_US", "VERIFIED", "ELIGIBLE", "KNOWN", "[]"),
            ("test-1", "owner_controlled_test", "COUNTRY_UNKNOWN", "OWNER_CONTROLLED_TEST", "OWNER_CONTROLLED_TEST", "SIZE_UNKNOWN", "[]"),
        ]
        for pid, ptype, geo, identity, qual, size, channels in values:
            website = "https://hotel.example" if pid == "hotel-1" else "https://example.com"
            contact_status = "CONTACT_VERIFIED" if pid in {"company-1", "hotel-1"} else "NOT_VERIFIED"
            db.execute("""INSERT INTO research_records(profile_id,profile_version,profile_type,name,official_website,industry,location,size_status,size_evidence_json,verification_evidence_json,provenance_json,contact_channels_json,cohort_membership_json,identity_verification_status,geographic_classification,geographic_evidence_json,contact_verification_status,screening_status,recruitment_priority,outreach_ready_status,qualification_status,recruitment_status,questionnaire_status,consent_status,geo_research_status,monitoring_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (pid,"v1",ptype,pid,website,"test","US","","[]","[]","[]",channels,"{}",identity,geo,"{}",contact_status,"NOT_SCREENED","NEEDS_REVIEW","NOT_READY",qual,"NOT_INVITED","NOT_INVITED","NOT_ASKED","NOT_ELIGIBLE","NOT_STARTED","now","now"))
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


def test_contact_gate_rejects_pending_unknown_unsupported_and_restricted_addresses(tmp_path):
    path = _db(tmp_path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("select * from research_records where profile_id='company-1'").fetchone()
        assert deterministic_gate(row)[0] == "ELIGIBLE_FOR_AI_SCREENING"
        db.execute("update research_records set contact_verification_status='NOT_VERIFIED',contact_channels_json=? where profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"owner@example.com","source_url":"https://example.com/contact","evidence":"Email owner@example.com for business inquiries","review_status":"PENDING"}]',))
        pending = db.execute("select * from research_records where profile_id='company-1'").fetchone()
        assert "BUSINESS_EMAIL_PENDING_VERIFICATION" in deterministic_gate(pending)[1]
        db.execute("update research_records set contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? where profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"support@example.com","source_url":"https://example.com/contact","evidence":"Contact support@example.com for help desk support","review_status":"VERIFIED"}]',))
        restricted = db.execute("select * from research_records where profile_id='company-1'").fetchone()
        assert "NO_VERIFIED_FIRST_PARTY_BUSINESS_EMAIL" in deterministic_gate(restricted)[1]
        db.execute("update research_records set contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? where profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"owner@other.example","source_url":"https://other.example/contact","evidence":"Email owner@other.example for business inquiries","review_status":"VERIFIED"}]',))
        unsupported = db.execute("select * from research_records where profile_id='company-1'").fetchone()
        assert "NO_VERIFIED_FIRST_PARTY_BUSINESS_EMAIL" in deterministic_gate(unsupported)[1]
        db.execute("update research_records set contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? where profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"owner@example.com","source_url":"https://example.com/contact","evidence":"Email owner@example.com for business inquiries","review_status":"VERIFIED","verification_status":"UNKNOWN"}]',))
        unknown = db.execute("select * from research_records where profile_id='company-1'").fetchone()
        assert deterministic_gate(unknown)[0] == "BLOCKED_DETERMINISTIC"
        assert "BUSINESS_EMAIL_PENDING_VERIFICATION" in deterministic_gate(unknown)[1]


def test_full_job_rechecks_contact_before_paid_provider_call(tmp_path, monkeypatch):
    path = _db(tmp_path)
    job_id = plan_full_run(path, run_key="contact-gate-regression")
    with sqlite3.connect(path) as db:
        db.execute("update full_screening_queue set status='SKIPPED_TEST' where job_id=? and profile_id<>'company-1'", (job_id,))
        db.execute("update full_screening_queue set status='PENDING_AI' where job_id=? and profile_id='company-1'", (job_id,))
        db.execute("update research_records set contact_verification_status='NOT_VERIFIED',contact_channels_json=? where profile_id='company-1'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"owner@example.com","source_url":"https://example.com/contact","evidence":"Email owner@example.com for business inquiries","review_status":"PENDING"}]',))
    monkeypatch.setattr("outreach_agent.full_screening.detect_provider", lambda: "OPENAI_COMPATIBLE")
    monkeypatch.setattr("outreach_agent.full_screening.configured_provider", lambda: SimpleNamespace(input_cost_per_million=0,output_cost_per_million=0,model="test"))
    monkeypatch.setattr("outreach_agent.preliminary_screening._fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("contact-ineligible record must not be fetched")))
    monkeypatch.setattr("outreach_agent.full_screening.call_ai", lambda *a, **k: (_ for _ in ()).throw(AssertionError("paid provider must not be called")))
    result = run_full_job(job_id, path)
    assert result["paid_calls"] == 0
    assert result["contact_skipped"] == 1
    with sqlite3.connect(path) as db:
        row = db.execute("select status,eligibility_status,last_error from full_screening_queue where job_id=? and profile_id='company-1'", (job_id,)).fetchone()
        assert row[0] == "DEFERRED_CONTACT_INELIGIBLE"
        assert row[1] == "BLOCKED_CONTACT_INELIGIBLE"
        assert "BUSINESS_EMAIL_PENDING_VERIFICATION" in row[2]
        assert db.execute("select count(*) from full_screening_queue where job_id=?",(job_id,)).fetchone()[0]==4
        assert db.execute("select attempts from full_screening_queue where job_id=? and profile_id='company-1'",(job_id,)).fetchone()[0]==0


def test_existing_personalization_is_preserved_and_never_queued_for_paid_repeat(tmp_path,monkeypatch):
    path=_db(tmp_path)
    original='{"existing":"paid work"}'
    with sqlite3.connect(path) as db:
        db.execute("update research_records set personalization_status='COMPLETE',personalization_json=? where profile_id='company-1'",(original,))
    job_id=plan_full_run(path,run_key="already-personalized")
    with sqlite3.connect(path) as db:
        queued=db.execute("select status,blocking_reasons_json from full_screening_queue where job_id=? and profile_id='company-1'",(job_id,)).fetchone()
        assert queued[0]=="BLOCKED_DETERMINISTIC"
        assert "PERSONALIZATION_ALREADY_COMPLETE" in queued[1]
        initial_members=db.execute("select count(*) from full_screening_queue where job_id=?",(job_id,)).fetchone()[0]
    monkeypatch.setattr("outreach_agent.full_screening.detect_provider",lambda:"UNCONFIGURED")
    result=run_full_job(job_id,path)
    assert result["paid_calls"]==0
    with sqlite3.connect(path) as db:
        assert db.execute("select personalization_status,personalization_json from research_records where profile_id='company-1'").fetchone()==("COMPLETE",original)
        assert db.execute("select count(*) from full_screening_queue where job_id=?",(job_id,)).fetchone()[0]==initial_members
