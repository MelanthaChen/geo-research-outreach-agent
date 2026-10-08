import csv
import hashlib
import importlib.util
from pathlib import Path

import pytest
import yaml

from outreach_agent.outreach_pipeline import (
    ApprovalGate,
    DemoTransport,
    OutreachState,
    SuppressionRegistry,
    classify_response,
    evaluate_delivery_gate,
    form_submission_safety,
    idempotency_key,
    map_contact_form_fields,
    prepare_signup_handoff,
    render_template,
    transition,
    validate_recipient,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phase5a_outreach_demo", ROOT / "scripts" / "phase5a_outreach_demo.py")
demo_script = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(demo_script)


def test_template_rendering_and_missing_variables():
    assert render_template("Hi {company_name}: {signup}", {"company_name": "Example", "signup": ""}) == "Hi Example:"
    with pytest.raises(ValueError, match="missing template variables: affiliation"):
        render_template("Hello {affiliation}", {})


def test_default_sender_and_signup_configuration_is_unset():
    config = yaml.safe_load((ROOT / "config/outreach.yaml").read_text())
    assert config["sender_name"] == ""
    assert config["sender_email"] == ""
    assert config["reply_to_email"] == ""
    assert config["research_signup_url"] == ""
    assert config["research_signup_url_approved"] is False
    assert config["live_sending_enabled"] is False


def test_recipient_validation_normalizes_and_rejects_multiple_or_malformed_addresses():
    assert validate_recipient("Person@Example.org") == "person@example.org"
    for bad in ("", "not-an-email", "first@example.org,second@example.org", "x@y", "Name <x@example.org>"):
        with pytest.raises(ValueError):
            validate_recipient(bad)


def test_approval_gate_requires_explicit_human_approval_for_real_delivery_and_live_adapter_is_absent():
    pending = ApprovalGate(
        identity_verified=True, eligibility="ELIGIBLE", channel_type="GENERAL_BUSINESS_EMAIL", recipient_valid=True,
        company_review_status="PENDING", channel_review_status="PENDING",
    )
    assert evaluate_delivery_gate(pending) == "BLOCKED_COMPANY_REVIEW"
    assert evaluate_delivery_gate(pending, demo_fixture_approval=True) == "SIMULATED_APPROVAL_ONLY"
    assert evaluate_delivery_gate(ApprovalGate(
        identity_verified=False, eligibility="ELIGIBLE", channel_type="GENERAL_BUSINESS_EMAIL", recipient_valid=True,
        company_review_status="APPROVED", channel_review_status="APPROVED",
    )) == "BLOCKED_IDENTITY_UNVERIFIED"
    all_human_approvals = ApprovalGate(
        identity_verified=True, eligibility="ELIGIBLE", channel_type="GENERAL_BUSINESS_EMAIL", recipient_valid=True,
        company_review_status="APPROVED", channel_review_status="APPROVED", real_outreach_approval=True,
        message_approved=True, sender_configured=True, live_sending_enabled=True,
    )
    assert evaluate_delivery_gate(all_human_approvals) == "BLOCKED_NO_LIVE_PROVIDER_IN_PHASE_5A"


def test_demo_transport_is_idempotent_and_recovers_after_simulated_failure():
    transport = DemoTransport()
    kwargs = dict(company_id=5, contact_id=10, recipient="info@example.org", subject="Invitation", body="Draft",
                  approval_status="SIMULATED_APPROVAL_ONLY")
    key = idempotency_key(5, 10, "info@example.org", "Invitation", "Draft")
    failed = transport.deliver(**kwargs, simulate_failure=True)
    assert failed.status == "SIMULATED_FAILED"
    assert failed.idempotency_key == key
    delivered = transport.deliver(**kwargs)
    assert delivered.status == "SIMULATED_DELIVERED"
    duplicate = transport.deliver(**kwargs)
    assert duplicate.status == "SIMULATED_DUPLICATE_SUPPRESSED"
    assert duplicate.provider_message_id == delivered.provider_message_id
    assert delivered.provider_message_id.startswith("SIMULATED-")


def test_opt_out_suppression_persists_real_entries_but_not_simulated_responses(tmp_path):
    path = tmp_path / "suppression.csv"
    registry = SuppressionRegistry.load(path)
    registry.suppress("Stop@Example.org", reason="explicit opt-out", source="reply", simulated=False)
    assert SuppressionRegistry.load(path).is_suppressed("stop@example.org")
    registry.suppress("simulated@example.org", reason="fixture", source="demo", simulated=True)
    assert not SuppressionRegistry.load(path).is_suppressed("simulated@example.org")
    opted_out_gate = ApprovalGate(
        identity_verified=True, eligibility="ELIGIBLE", channel_type="GENERAL_BUSINESS_EMAIL", recipient_valid=True,
        company_review_status="APPROVED", channel_review_status="APPROVED", opted_out=True,
    )
    assert evaluate_delivery_gate(opted_out_gate) == "SUPPRESSED_OPT_OUT"


def test_state_transitions_are_auditable_idempotent_and_reject_skips():
    state = OutreachState.NOT_PREPARED
    state = transition(state, OutreachState.DRAFT_READY)
    assert transition(state, state) == state
    state = transition(state, OutreachState.NEEDS_REVIEW)
    with pytest.raises(ValueError, match="invalid outreach state"):
        transition(state, OutreachState.SENT)
    state = transition(state, OutreachState.APPROVED)
    state = transition(state, OutreachState.SENDING)
    state = transition(state, OutreachState.FAILED)
    state = transition(state, OutreachState.APPROVED)
    assert state == OutreachState.APPROVED


def test_contact_form_field_mapping_captcha_and_consent_are_never_submitted():
    fields = map_contact_form_fields("Name Email Phone Company Subject Message")
    assert set(fields) == {"name", "email", "phone", "company", "subject", "message"}
    safety = form_submission_safety("Email Message reCAPTCHA I agree to the privacy policy")
    assert safety == {
        "captcha_detected": "true", "consent_detected": "true",
        "submission_status": "NOT_SUBMITTED_DEMO", "manual_confirmation_required": "true",
    }


def test_response_classification_interest_is_not_consent_and_signup_needs_approval():
    assert classify_response("INTERESTED") == OutreachState.INTERESTED
    assert classify_response("OPTED_OUT") == OutreachState.OPTED_OUT
    assert prepare_signup_handoff(current_state=OutreachState.RESPONSE_RECEIVED, signup_url="https://research.example.org", signup_url_approval_fixture=True)["status"] == "BLOCKED_NO_EXPLICIT_INTEREST"
    missing = prepare_signup_handoff(current_state=OutreachState.INTERESTED, signup_url="", signup_url_approval_fixture=False)
    assert missing["status"] == "NEEDS_CONFIGURED_AND_APPROVED_SIGNUP_URL"
    handoff = prepare_signup_handoff(current_state=OutreachState.INTERESTED, signup_url="https://research.example.invalid/demo", signup_url_approval_fixture=True, demo=True)
    assert handoff == {"status": "SIMULATED_HANDOFF_READY", "url": "https://research.example.invalid/demo", "consent_status": "NOT_ESTABLISHED"}


def test_offline_demo_is_reproducible_and_does_not_write_database_or_approval_states(tmp_path):
    database = ROOT / "data/phase4f_experimental.db"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    summary = demo_script.run_demo(database, tmp_path, max_draft_companies=10)
    first = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob("*.csv")}
    second_summary = demo_script.run_demo(database, tmp_path, max_draft_companies=10)
    second = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.glob("*.csv")}
    assert first == second
    assert summary == second_summary
    assert summary == {
        "cohort_companies": 537, "draft_sample_companies": 10, "email_drafts": 9,
        "contact_form_drafts": 1, "no_channel_scenarios": 1, "simulated_deliveries": 1,
        "simulated_responses": 1, "simulated_signup_handoffs": 1, "real_sends": 0,
        "real_form_submissions": 0, "database_writes": 0,
    }
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with (tmp_path / "phase5a_outreach_candidates.csv").open(newline="", encoding="utf-8") as handle:
        candidates = list(csv.DictReader(handle))
    assert {(row["company_name"], row["candidate_status"]) for row in candidates} >= {
        ("Vexo", "SUITABLE_CHANNEL_DRAFT_SAMPLE"),
        ("AnswerThis", "SUITABLE_CHANNEL_DRAFT_SAMPLE"),
        ("CodeWisp", "NO_SUITABLE_CHANNEL — NO DRAFT OR DELIVERY"),
    }
    with (tmp_path / "phase5a_delivery_simulation.csv").open(newline="", encoding="utf-8") as handle:
        delivery = list(csv.DictReader(handle))
    assert all(row["real_outreach_approval_status"] == "NOT_GRANTED" for row in delivery)
    assert all(row["research_consent_status"] == "NOT_ESTABLISHED" for row in delivery)
    assert not any(row["form_submission_status"] not in {"NOT_SUBMITTED_DEMO", "NOT_APPLICABLE"} for row in delivery)
