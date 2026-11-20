import json
import sqlite3

from outreach_agent.contact_recovery import contact_readiness_summary, recover_personalized_contacts
from outreach_agent.research_lifecycle import register_profile


def _profile(profile_id, website, email):
    return {
        "schema_version": "geo-company-profile/v1",
        "profile_id": profile_id,
        "profile_type": "startup_company",
        "identity": {"name": profile_id, "website": website, "verification_status": "VERIFIED", "verification_evidence": [{"url": website + "/about"}]},
        "startup": {"industry": "software", "location": "Boston, MA"},
        "qualification": {"eligibility": "ELIGIBLE"},
        "provenance": {"source_name": "test fixture", "source_batch": "isolated"},
        "contact_channels": [{"channel_type":"GENERAL_BUSINESS_EMAIL","email":email,"source_url":website+"/contact","evidence":f"Business inquiries: {email}","review_status":"PENDING"}],
    }


def test_bounded_recovery_reuses_saved_evidence_and_preserves_personalization(tmp_path):
    path=tmp_path/"registry.db"
    register_profile(_profile("stored-contact","https://stored.org","hello@stored.org"),path)
    payload={"data_classification":"OBSERVED","business_description":"Existing paid personalization","company_strength":"Existing work preserved","source_urls":["https://stored.org/about"],"recommendations":[]}
    with sqlite3.connect(path) as db:
        db.execute("update research_records set personalization_status='COMPLETE',personalization_json=?,contact_verification_status='NOT_VERIFIED' where profile_id='stored-contact'",(json.dumps(payload),))
    before=contact_readiness_summary(path)
    assert before=={"eligible_for_invitation_drafting":1,"no_eligible_contact":0}
    result=recover_personalized_contacts(path,limit=1,profile_ids=["stored-contact"])
    assert result["selected"]==1 and result["recovered"]==1 and result["pending"]==0
    assert result["paid_calls"]==0 and result["network_requests"]==0
    with sqlite3.connect(path) as db:
        row=db.execute("select contact_verification_status,personalization_status,personalization_json,contact_channels_json from research_records where profile_id='stored-contact'").fetchone()
        assert row[0:2]==("CONTACT_VERIFIED","COMPLETE")
        assert json.loads(row[2])==payload
        channel=json.loads(row[3])[0]
        assert channel["review_status"]=="VERIFIED"
        assert channel["verification_method"]=="STORED_FIRST_PARTY_EXACT_EMAIL_EVIDENCE"
        assert db.execute("select outcome,method from contact_recovery_audit where profile_id='stored-contact'").fetchone()==("RECOVERED","STORED_FIRST_PARTY_EXACT_EMAIL_EVIDENCE")
    repeated=recover_personalized_contacts(path,limit=1,profile_ids=["stored-contact"])
    assert repeated["selected"]==0 and repeated["recovered"]==0
    counts=contact_readiness_summary(path)
    assert counts=={"eligible_for_invitation_drafting":1,"no_eligible_contact":0}


def test_recovery_never_verifies_nonfirstparty_restricted_or_missing_evidence(tmp_path):
    path=tmp_path/"registry.db"
    rows=[
        ("external","https://brand.example","owner@other.example","https://directory.example/contact","owner@other.example for business"),
        ("support","https://support.example","support@support.example","https://support.example/contact","support@support.example for help desk support"),
        ("missing-evidence","https://missing.example","hello@missing.example","https://missing.example/contact","Email us for information"),
    ]
    for profile_id,website,email,source,evidence in rows:
        register_profile(_profile(profile_id,website,email),path)
        with sqlite3.connect(path) as db:
            channels=[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":email,"source_url":source,"evidence":evidence,"review_status":"PENDING"}]
            db.execute("update research_records set personalization_status='COMPLETE',personalization_json='{}',contact_verification_status='NOT_VERIFIED',contact_channels_json=? where profile_id=?",(json.dumps(channels),profile_id))
    result=recover_personalized_contacts(path,limit=3)
    assert result["selected"]==3 and result["recovered"]==0 and result["pending"]==3
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from research_records where contact_verification_status='CONTACT_VERIFIED'").fetchone()[0]==0
        assert db.execute("select count(*) from contact_recovery_audit where outcome='PENDING'").fetchone()[0]==3
