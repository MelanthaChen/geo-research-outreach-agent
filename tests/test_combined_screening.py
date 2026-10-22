import sqlite3

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


def test_one_analysis_pass_persists_findings_and_draft_without_second_provider_call(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    register_profile(_profile(), path)
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
