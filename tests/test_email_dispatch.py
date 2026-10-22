import sqlite3

import pytest

from outreach_agent.email_dispatch import (
    DispatchConfig, EmailProviderError, ProviderResult, approve_and_dispatch_batch,
    dispatch_approved_drafts,
)
from outreach_agent.microsoft_graph_integration import MicrosoftGraphProvider, verify_sender_identity
from outreach_agent.research_lifecycle import create_email_draft, create_questionnaire_invitation, register_profile


def profile(profile_id="dispatch-company"):
    return {"schema_version": "geo-company-profile/v1", "profile_id": profile_id, "profile_type": "startup_company", "identity": {"name": "Dispatch Co", "website": "https://dispatch.example", "verification_status": "VERIFIED"}, "startup": {"location": "Austin, TX", "industry": "Software"}, "qualification": {"eligibility": "ELIGIBLE"}}


class FakeProvider:
    name = "fake"
    enabled = True

    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            self.failures -= 1
            raise EmailProviderError("temporary provider failure", retryable=True)
        return ProviderResult("provider-message-1")


def make_draft(db, profile_id="dispatch-company", recipient="owner@example.test"):
    register_profile(profile(profile_id), db)
    invitation_id, _ = create_questionnaire_invitation(profile_id, secret="secret", path=db)
    draft = create_email_draft(profile_id, recipient, "Subject body", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft))
    return draft


def test_single_and_batch_use_same_approval_gated_dispatch_path(tmp_path):
    db = tmp_path / "registry.db"
    first = make_draft(db)
    provider = FakeProvider()
    assert dispatch_approved_drafts([first], provider=provider, sender="sender@example.test", path=db)[0].status == "BLOCKED_APPROVAL"
    second = make_draft(db, "dispatch-company-2", "second@example.test")
    outcomes = approve_and_dispatch_batch([first, second], "human-reviewer", provider=provider, sender="sender@example.test", path=db, config=DispatchConfig(allowed_recipients=frozenset({"owner@example.test", "second@example.test"})))
    assert [item.status for item in outcomes] == ["SENT", "SENT"]
    assert len(provider.calls) == 2


def test_allowlist_rate_limit_and_duplicate_prevention(tmp_path):
    db = tmp_path / "registry.db"
    draft = make_draft(db, recipient="not-allowed@example.test")
    provider = FakeProvider()
    outcome = approve_and_dispatch_batch([draft], "reviewer", provider=provider, sender="sender@example.test", path=db, config=DispatchConfig(allowed_recipients=frozenset({"owner@example.test"})))
    assert outcome[0].status == "BLOCKED_ALLOWLIST"
    assert not provider.calls

    allowed = make_draft(db, "dispatch-company-allowed", "owner@example.test")
    sent = approve_and_dispatch_batch([allowed], "reviewer", provider=provider, sender="sender@example.test", path=db, config=DispatchConfig(allowed_recipients=frozenset({"owner@example.test"})))
    duplicate = dispatch_approved_drafts([allowed], provider=provider, sender="sender@example.test", path=db, config=DispatchConfig(allowed_recipients=frozenset({"owner@example.test"})))
    assert sent[0].status == "SENT" and duplicate[0].status == "DUPLICATE"
    assert len(provider.calls) == 1


def test_rate_limit_blocks_second_message_without_provider_call(tmp_path):
    db = tmp_path / "registry.db"
    first = make_draft(db, "rate-company-1", "one@example.test")
    second = make_draft(db, "rate-company-2", "two@example.test")
    provider = FakeProvider()
    outcomes = approve_and_dispatch_batch([first, second], "reviewer", provider=provider, sender="sender@example.test", path=db, config=DispatchConfig(max_per_minute=1, allowed_recipients=frozenset({"one@example.test", "two@example.test"})))
    assert [item.status for item in outcomes] == ["SENT", "RATE_LIMITED"]
    assert len(provider.calls) == 1


def test_retry_and_failure_are_persisted(tmp_path):
    db = tmp_path / "registry.db"
    draft = make_draft(db)
    provider = FakeProvider(failures=2)
    outcome = approve_and_dispatch_batch([draft], "reviewer", provider=provider, sender="sender@example.test", path=db, config=DispatchConfig(max_attempts=3))
    assert outcome[0].status == "SENT" and outcome[0].attempts == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM dispatch_attempts WHERE draft_id=?", (draft,)).fetchone()[0] == 3


def test_graph_sender_identity_and_provider_failure_recovery():
    assert verify_sender_identity({"preferred_username": "person@northeastern.edu"}, "person@northeastern.edu")
    assert not verify_sender_identity({"preferred_username": "other@example.com"}, "person@northeastern.edu")
    responses = [type("Response", (), {"status_code": 503, "headers": {}})(), type("Response", (), {"status_code": 202, "headers": {"request-id": "req-1"}})()]
    def transport(*args, **kwargs):
        return responses.pop(0)
    provider = MicrosoftGraphProvider(access_token="test-token", authenticated_sender="person@northeastern.edu", transport=transport)
    with pytest.raises(EmailProviderError, match="HTTP 503") as exc:
        provider.send(sender="person@northeastern.edu", recipient="owner@example.test", subject="s", body="b")
    assert exc.value.retryable
    assert provider.send(sender="person@northeastern.edu", recipient="owner@example.test", subject="s", body="b").message_id == "req-1"
    with pytest.raises(EmailProviderError, match="sender identity"):
        provider.send(sender="wrong@example.com", recipient="owner@example.test", subject="s", body="b")
