import sqlite3

from outreach_agent.research_lifecycle import (
    approve_invitation_batch,
    generate_invitation_batch,
    get_record,
    init_registry,
    register_profile,
)


def profile(profile_id: str, *, email: str | None = "hello@example.test", owner_test: bool = False) -> dict:
    return {
        "schema_version": "geo-company-profile/v1",
        "profile_id": profile_id,
        "profile_type": "owner_controlled_test" if owner_test else "startup_company",
        "identity": {
            "name": "Owner Test" if owner_test else "Cedar Studio",
            "website": "https://cedar.example",
            "verification_status": "VERIFIED",
            "verification_evidence": [{"url": "https://cedar.example/about", "text": "Cedar Studio"}],
        },
        "startup": {"industry": "design services", "location": "Austin, TX"},
        "qualification": {"eligibility": "ELIGIBLE"},
        "provenance": {"source_name": "Geofabrik US extract", "source_batch": "us"},
        "contact_channels": ([{"channel_type": "GENERAL_BUSINESS_EMAIL", "email": email,
                               "source_url": "https://cedar.example/contact", "review_status": "VERIFIED"}]
                             if email else []),
    }


def test_batch_generation_is_bounded_idempotent_and_never_sends(tmp_path):
    path = tmp_path / "registry.db"
    register_profile(profile("company-ready"), path)
    register_profile(profile("company-no-contact", email=None), path)
    register_profile(profile("owner-fixture", owner_test=True), path)

    result = generate_invitation_batch(
        ["company-ready", "company-no-contact", "owner-fixture", "company-ready"],
        secret="test-secret",
        path=path,
    )
    assert result["summary"] == {
        "total_selected": 3,
        "successfully_generated": 1,
        "ready_for_approval": 1,
        "discovered_unverified_recipient": 0,
        "missing_verified_recipient": 1,
        "insufficient_verified_evidence": 0,
        "duplicate_already_invited": 0,
        "owner_controlled_test_excluded": 1,
        "generation_failed": 0,
    }
    assert len(result["draft_ids"]) == 1
    assert get_record("owner-fixture", path)["email_drafts"] == []
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from email_drafts").fetchone()[0] == 1
        assert db.execute("select count(*) from invitation_batches").fetchone()[0] == 1

    repeated = generate_invitation_batch(["company-ready"], secret="test-secret", path=path)
    assert repeated["summary"]["duplicate_already_invited"] == 1
    assert repeated["draft_ids"] == result["draft_ids"]
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from email_drafts").fetchone()[0] == 1


def test_pending_first_party_business_email_can_prepare_draft_without_verification(tmp_path):
    path = tmp_path / "registry.db"
    pending = profile("company-pending")
    pending["contact_channels"][0]["review_status"] = "PENDING"
    register_profile(pending, path)

    result = generate_invitation_batch(["company-pending"], secret="test-secret", path=path)

    assert result["summary"]["successfully_generated"] == 1
    assert result["summary"]["ready_for_approval"] == 1
    assert result["summary"]["discovered_unverified_recipient"] == 1
    record = get_record("company-pending", path)
    assert record["contact_channels"][0]["review_status"] == "PENDING"
    assert record["email_drafts"][0]["approval_status"] == "PENDING"


def test_purpose_specific_email_is_excluded_from_draft_preparation(tmp_path):
    path = tmp_path / "registry.db"
    blocked = profile("company-blocked", email="careers@cedar.example")
    blocked["contact_channels"][0]["review_status"] = "PENDING"
    register_profile(blocked, path)

    result = generate_invitation_batch(["company-blocked"], secret="test-secret", path=path)

    assert result["summary"]["successfully_generated"] == 0
    assert result["summary"]["missing_verified_recipient"] == 1
    assert get_record("company-blocked", path)["email_drafts"] == []


def test_bound_owner_test_recipients_use_normal_batch_workflow(tmp_path):
    path = tmp_path / "registry.db"
    register_profile(profile("owner-one", owner_test=True), path)
    register_profile(profile("owner-two", owner_test=True), path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO owner_test_recipient_bindings(profile_id,recipient,label,created_at,updated_at) VALUES (?,?,?,?,?)", ("owner-one", "yuxuan.chen.1031@outlook.com", "GeoAIResume demo", "now", "now"))
        db.execute("INSERT INTO owner_test_recipient_bindings(profile_id,recipient,label,created_at,updated_at) VALUES (?,?,?,?,?)", ("owner-two", "yuxaun.chen.1031@outlook.com", "Whisker Haven demo", "now", "now"))
    result = generate_invitation_batch(["owner-one", "owner-two"], secret="test-secret", path=path)
    assert result["summary"]["successfully_generated"] == 2
    assert result["summary"]["owner_controlled_test_excluded"] == 0
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT delivery_recipient FROM email_drafts ORDER BY profile_id").fetchall()
    assert [row[0] for row in rows] == ["yuxuan.chen.1031@outlook.com", "yuxaun.chen.1031@outlook.com"]


def test_batch_approval_requires_explicit_reviewer_and_is_idempotent(tmp_path):
    path = tmp_path / "registry.db"
    register_profile(profile("company-ready"), path)
    generated = generate_invitation_batch(["company-ready"], secret="test-secret", path=path)
    try:
        approve_invitation_batch(generated["batch_id"], "", path=path)
    except ValueError as exc:
        assert "reviewer" in str(exc)
    else:
        raise AssertionError("blank reviewer must be rejected")
    approved = approve_invitation_batch(generated["batch_id"], "operator", path=path)
    assert approved["status"] == "APPROVED"
    rerun = generate_invitation_batch(["company-ready"], secret="test-secret", path=path)
    assert rerun["batch_id"] == generated["batch_id"]
    again = approve_invitation_batch(generated["batch_id"], "operator", path=path)
    assert again["status"] == "EXISTING"
    record = get_record("company-ready", path)
    assert record["email_drafts"][0]["approval_status"] == "APPROVED"
    init_registry(path)
