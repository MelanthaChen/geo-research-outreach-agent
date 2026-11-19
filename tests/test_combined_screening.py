import sqlite3
import json

import pytest

from outreach_agent.combined_screening import run_combined_analysis
from outreach_agent.research_lifecycle import approve_email_batch, get_record, register_profile


def _profile():
    return {
        "schema_version": "geo-company-profile/v1",
        "profile_id": "company-combined-1",
        "profile_type": "startup_company",
        "identity": {"name": "Cedar Studio", "website": "https://cedar.example", "verification_status": "VERIFIED", "verification_evidence": [{"url": "https://cedar.example"}]},
        "startup": {"industry": "design services", "location": "Austin, TX"},
        "qualification": {"eligibility": "ELIGIBLE"},
        "provenance": {"source_name": "Geofabrik US extract", "source_batch": "us"},
        "contact_channels": [{"channel_type": "GENERAL_BUSINESS_EMAIL", "email": "hello@cedar.example", "source_url": "https://cedar.example/contact"}],
    }


def _verify_contact(path, profile_id="company-combined-1", email="hello@cedar.example"):
    channels = [{"channel_type":"GENERAL_BUSINESS_EMAIL","email":email,"source_url":"https://cedar.example/contact","evidence":f"For business inquiries contact {email}","review_status":"VERIFIED","verification_status":"VERIFIED"}]
    with sqlite3.connect(path) as db:
        db.execute("update research_records set contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? where profile_id=?", (json.dumps(channels),profile_id))


def test_one_analysis_pass_persists_findings_and_draft_without_second_provider_call(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    register_profile(_profile(), path)
    _verify_contact(path)
    calls = {"fetch": 0, "ai": 0}

    def fetch(*args, **kwargs):
        calls["fetch"] += 1
        return ([{"url": "https://cedar.example/services", "title": "Services", "text": "Cedar Studio in Austin, TX offers design services. Contact us for hours, services, and project information."}], {"robots": "ALLOWED", "http_status": "200"})

    monkeypatch.setattr("outreach_agent.combined_screening._fetch", fetch)
    monkeypatch.setattr("outreach_agent.combined_screening.call_ai", lambda *a, **k: calls.__setitem__("ai", calls["ai"] + 1))
    first = run_combined_analysis("company-combined-1", mode="RULE_BASED", recipient="hello@cedar.example", secret="test-secret", path=path, base_url="https://research.example")
    assert first["status"] == "DRAFTED"
    assert calls == {"fetch": 1, "ai": 0}
    record = get_record("company-combined-1", path)
    assert len(record["analysis_results"]) == 1
    assert len(record["email_drafts"]) == 1
    assert record["email_drafts"][0]["approval_status"] == "PENDING"
    assert "docs.google.com/forms/d/e/" in record["email_drafts"][0]["body"]
    assert "entry.1773870597=" in record["email_drafts"][0]["body"]
    assert first["invitation_reference"].startswith("GEO-")

    monkeypatch.setattr("outreach_agent.combined_screening._fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cached evidence must be reused")))
    second = run_combined_analysis("company-combined-1", mode="RULE_BASED", recipient="hello@cedar.example", secret="test-secret", path=path, base_url="https://research.example")
    assert second["status"] == "IDEMPOTENT_EXISTING"
    assert calls == {"fetch": 1, "ai": 0}
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from screening_analysis_results").fetchone()[0] == 1
        assert db.execute("select count(*) from email_drafts").fetchone()[0] == 1
    approve_email_batch([first["draft_id"]], "reviewer", path=path)
    assert get_record("company-combined-1", path)["email_drafts"][0]["approval_status"] == "APPROVED"


def test_combined_invitation_uses_mail_merge_safe_subject_and_personalized_url(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    profile = _profile()
    profile["profile_id"] = "company-punctuation-1"
    profile["identity"]["name"] = "Cedar Studio — Boston"
    register_profile(profile, path)
    _verify_contact(path,"company-punctuation-1")

    monkeypatch.setattr(
        "outreach_agent.combined_screening._fetch",
        lambda *args, **kwargs: ([{
            "url": "https://cedar.example/services",
            "title": "Services",
            "text": "Cedar Studio Boston offers design services and contact information.",
        }], {"robots": "ALLOWED", "http_status": "200"}),
    )
    result = run_combined_analysis(
        "company-punctuation-1",
        mode="RULE_BASED",
        recipient="hello@cedar.example",
        secret="test-secret",
        path=path,
        base_url="https://research.example",
    )
    draft = get_record("company-punctuation-1", path)["email_drafts"][0]
    assert result["status"] == "DRAFTED"
    assert "—" not in draft["subject"]
    assert "entry.1773870597=" in draft["body"]
    assert "GEO-" in draft["body"]
    assert "no email has been sent" not in draft["body"].casefold()


def test_unverified_contact_blocks_paid_personalization_before_fetch_or_provider(tmp_path, monkeypatch):
    path=tmp_path/"registry.db"
    register_profile(_profile(),path)
    calls={"fetch":0,"ai":0}
    monkeypatch.setattr("outreach_agent.combined_screening._fetch",lambda *a,**k:calls.__setitem__("fetch",calls["fetch"]+1))
    monkeypatch.setattr("outreach_agent.combined_screening.configured_provider",lambda:object())
    monkeypatch.setattr("outreach_agent.combined_screening.call_ai",lambda *a,**k:calls.__setitem__("ai",calls["ai"]+1))
    with pytest.raises(PermissionError,match="verified, evidence-matched first-party business email"):
        run_combined_analysis("company-combined-1",mode="PAID_API",recipient="hello@cedar.example",secret="test-secret",path=path)
    assert calls=={"fetch":0,"ai":0}


@pytest.mark.parametrize("field,value",[("qualification_status","NEEDS_REVIEW"),("suppression_status","SUPPRESSED"),("withdrawal_status","WITHDRAWN"),("recruitment_status","UNSUBSCRIBED")])
def test_qualification_and_suppression_checks_precede_paid_personalization(tmp_path,monkeypatch,field,value):
    path=tmp_path/f"{field}.db"
    register_profile(_profile(),path)
    _verify_contact(path)
    with sqlite3.connect(path) as db:
        db.execute(f"update research_records set {field}=? where profile_id='company-combined-1'",(value,))
    calls={"fetch":0,"ai":0}
    monkeypatch.setattr("outreach_agent.combined_screening._fetch",lambda *a,**k:calls.__setitem__("fetch",calls["fetch"]+1))
    monkeypatch.setattr("outreach_agent.combined_screening.configured_provider",lambda:object())
    monkeypatch.setattr("outreach_agent.combined_screening.call_ai",lambda *a,**k:calls.__setitem__("ai",calls["ai"]+1))
    with pytest.raises(PermissionError):
        run_combined_analysis("company-combined-1",mode="PAID_API",recipient="hello@cedar.example",secret="test-secret",path=path)
    assert calls=={"fetch":0,"ai":0}


def test_multiple_verified_recipients_require_explicit_choice_for_batch(tmp_path, monkeypatch):
    from outreach_agent.research_lifecycle import generate_invitation_batch
    path=tmp_path/"registry.db"
    register_profile(_profile(),path)
    _verify_contact(path)
    channels=[
        {"channel_type":"GENERAL_BUSINESS_EMAIL","email":"hello@cedar.example","source_url":"https://cedar.example/contact","evidence":"hello@cedar.example business inquiries","review_status":"VERIFIED"},
        {"channel_type":"PARTNERSHIP_EMAIL","email":"partners@cedar.example","source_url":"https://cedar.example/contact","evidence":"partners@cedar.example partnership inquiries","review_status":"APPROVED"},
    ]
    with sqlite3.connect(path) as db:
        db.execute("update research_records set contact_channels_json=? where profile_id='company-combined-1'",(json.dumps(channels),))
    created=[]
    def draft(profile_id,*,delivery_recipient,**kwargs):
        created.append(delivery_recipient)
        return {"status":"DRAFTED","draft_id":"draft-1","body":"review"}
    monkeypatch.setattr("outreach_agent.research_lifecycle.create_manual_invitation_draft",draft)
    pending=generate_invitation_batch(["company-combined-1"],secret="test-secret",path=path)
    assert pending["summary"]["recipient_selection_required"]==1
    assert created==[]
    chosen=generate_invitation_batch(["company-combined-1"],secret="test-secret",path=path,recipient_selections={"company-combined-1":"partners@cedar.example"})
    assert chosen["summary"]["successfully_generated"]==1
    assert created==["partners@cedar.example"]
