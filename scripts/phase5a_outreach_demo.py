"""Offline contact review and professor-facing Phase 5A demo. No sender/provider exists."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import types
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from outreach_agent.config import Crawl4AIFallbackSettings, load_settings  # noqa: E402
from outreach_agent.contacts import enrich_contacts  # noqa: E402
from outreach_agent.database import create_db_engine, session_factory  # noqa: E402
from outreach_agent.models import Company, Contact, ReviewStatus, WebsiteSnapshot  # noqa: E402
from outreach_agent.normalization import normalize_domain  # noqa: E402
from outreach_agent.outreach_pipeline import (  # noqa: E402
    ApprovalGate,
    DemoTransport,
    OutreachState,
    SUITABLE_CHANNEL_TYPES,
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
from outreach_agent.website_identity import verify_company_website  # noqa: E402
from outreach_agent.wikidata import write_csv_atomic  # noqa: E402

DATA = ROOT / "data"
DB_PATH = DATA / "phase4f_experimental.db"
OUTREACH_CONFIG = ROOT / "config/outreach.yaml"
TEMPLATE_PATH = ROOT / "config/templates/research_invitation.txt"
SCENARIO_PATH = ROOT / "config/phase5a_demo_scenario.yaml"
IDENTITY_CSVS = (DATA / "phase4e_website_verification.csv", DATA / "phase4f_website_verification.csv")

OUTREACH_CANDIDATE_FIELDS = [
    "demo_label", "company_id", "company_name", "website", "eligibility", "geo_opportunity", "research_priority",
    "identity_verification_status", "company_review_status", "contact_status", "contact_id", "channel_type",
    "recipient_or_form_url", "channel_review_status", "channel_validation_status", "evidence_url", "evidence_excerpt",
    "candidate_status",
]
EMAIL_DRAFT_FIELDS = [
    "demo_label", "company_id", "company_name", "contact_id", "recipient", "sender_name", "sender_email", "reply_to_email",
    "subject", "body", "evidence_url", "evidence_excerpt", "company_review_status", "channel_review_status",
    "outreach_state", "approval_status", "signup_url_included", "idempotency_key",
]
FORM_DRAFT_FIELDS = [
    "demo_label", "company_id", "company_name", "contact_id", "form_url", "evidence_url", "visible_form_evidence",
    "field_mapping_json", "proposed_values_json", "captcha_detected", "consent_detected", "submission_status",
    "manual_confirmation_required", "company_review_status", "channel_review_status",
]
DELIVERY_FIELDS = [
    "demo_label", "company_id", "company_name", "contact_id", "delivery_mode", "delivery_status", "provider_message_id",
    "idempotency_key", "attempt_status", "simulated_approval_fixture", "real_company_review_status",
    "real_channel_review_status", "real_outreach_approval_status", "simulated_response_status",
    "simulated_response_text", "signup_handoff_status", "signup_url", "signup_completed", "research_consent_status",
    "outreach_state_trace", "form_submission_status",
]


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def stored_identity_statuses() -> dict[int, str]:
    result: dict[int, str] = {}
    for path in IDENTITY_CSVS:
        for row in read_csv(path):
            if row.get("company_id") and row.get("verification_status"):
                result[int(row["company_id"])] = row["verification_status"].upper()
    return result


def verify_saved_snapshot(connection: sqlite3.Connection, company: sqlite3.Row) -> str:
    snapshot_row = connection.execute(
        "SELECT * FROM website_snapshots WHERE company_id=? ORDER BY fetched_at DESC,id DESC LIMIT 1",
        (company["id"],),
    ).fetchone()
    if not snapshot_row:
        return verify_company_website(types.SimpleNamespace(**dict(company)), None).status
    page_rows = connection.execute("SELECT * FROM website_pages WHERE snapshot_id=? ORDER BY id", (snapshot_row["id"],)).fetchall()
    snapshot = types.SimpleNamespace(**dict(snapshot_row), pages=[types.SimpleNamespace(**dict(page)) for page in page_rows])
    return verify_company_website(types.SimpleNamespace(**dict(company)), snapshot).status


def load_local_cohort(database: Path) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    if database.name != "phase4f_experimental.db":
        raise ValueError("Phase 5A demo is restricted to data/phase4f_experimental.db; frozen databases are not accepted")
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        companies = connection.execute("SELECT * FROM companies ORDER BY id").fetchall()
        if len(companies) != 537:
            raise ValueError(f"expected the existing 537-company Phase 4F cohort; found {len(companies)}")
        saved_status = stored_identity_statuses()
        company_rows: dict[int, dict[str, Any]] = {}
        for row in companies:
            identity_status = saved_status.get(row["id"]) or verify_saved_snapshot(connection, row)
            company_rows[row["id"]] = dict(row) | {"identity_verification_status": identity_status}
        contact_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for contact in connection.execute("SELECT * FROM contacts ORDER BY company_id,id"):
            contact_rows[contact["company_id"]].append(dict(contact))
    return company_rows, contact_rows


def _is_first_party_channel(company: dict[str, Any], contact: dict[str, Any]) -> bool:
    channel_type = contact.get("channel_type") or ""
    if channel_type not in SUITABLE_CHANNEL_TYPES or contact.get("review_status") == "REJECTED":
        return False
    source_url = contact.get("source_url") or contact.get("channel_url") or ""
    if not source_url or normalize_domain(source_url) != company["normalized_domain"]:
        return False
    if channel_type == "CONTACT_FORM":
        return bool(contact.get("channel_url")) and normalize_domain(contact["channel_url"]) == company["normalized_domain"]
    email = (contact.get("email") or "").strip()
    return "@" in email and normalize_domain("https://" + email.rsplit("@", 1)[-1]) == company["normalized_domain"]


def select_demo_channels(companies: dict[int, dict[str, Any]], contacts: dict[int, list[dict[str, Any]]],
                         count: int, scenario: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for company_id, company in companies.items():
        if company["identity_verification_status"] != "VERIFIED" or company.get("eligibility") != "ELIGIBLE":
            continue
        for contact in contacts.get(company_id, []):
            if _is_first_party_channel(company, contact):
                eligible.append((company, contact))
    preferred_email_id = int(scenario["scenarios"]["business_email_company_id"])
    preferred_form_id = int(scenario["scenarios"]["contact_form_company_id"])
    email = next(((c, x) for c, x in eligible if c["id"] == preferred_email_id and x.get("email")), None)
    form = next(((c, x) for c, x in eligible if c["id"] == preferred_form_id and x.get("channel_type") == "CONTACT_FORM"), None)
    if email is None or form is None:
        raise ValueError("the deterministic Vexo email and AnswerThis official-form demo evidence is unavailable")
    selected = [email, form]
    seen = {email[0]["id"], form[0]["id"]}
    email_candidates = sorted(
        (pair for pair in eligible if pair[1].get("email") and pair[0]["id"] not in seen),
        key=lambda pair: (-float(pair[0].get("priority_score") or 0), pair[0]["id"], pair[1]["id"]),
    )
    form_candidates = sorted(
        (pair for pair in eligible if pair[1].get("channel_type") == "CONTACT_FORM" and pair[0]["id"] not in seen),
        key=lambda pair: (-float(pair[0].get("priority_score") or 0), pair[0]["id"], pair[1]["id"]),
    )
    # Fill the bounded presentation sample with emails first while retaining a form case.
    for pair in email_candidates + form_candidates:
        if len(selected) >= count:
            break
        if pair[0]["id"] not in seen:
            selected.append(pair)
            seen.add(pair[0]["id"])
    if len(selected) < 10:
        raise ValueError(f"only {len(selected)} eligible first-party channel companies available; demo requires 10")
    return sorted(selected, key=lambda pair: pair[0]["id"])


def invitation_values(config: dict[str, Any], company_name: str, *, signup_url: str = "",
                      signup_is_simulated: bool = False) -> dict[str, str]:
    defaults = {
        "researcher_name": "[RESEARCHER NAME NOT CONFIGURED]",
        "university_affiliation": "[UNIVERSITY AFFILIATION NOT CONFIGURED]",
        "research_project_description": "[RESEARCH PROJECT DESCRIPTION NOT CONFIGURED]",
        "reply_to_email": "[REPLY-TO ADDRESS NOT CONFIGURED]",
        "sender_name": "[SENDER NAME NOT CONFIGURED]",
        "company_name": company_name,
        "research_signup_section": "",
    }
    for key in ("researcher_name", "university_affiliation", "research_project_description", "reply_to_email", "sender_name"):
        if config.get(key):
            defaults[key] = str(config[key])
    if signup_url and signup_is_simulated:
        defaults["research_signup_section"] = f"\nDEMO / SIMULATED ONLY — reserved non-operational URL: {signup_url}"
    elif signup_url and config.get("research_signup_url_approved"):
        defaults["research_signup_section"] = f"\nIf you would like to review the study information: {signup_url}"
    return defaults


def render_invitation(config: dict[str, Any], company_name: str, *, signup_url: str = "",
                      signup_is_simulated: bool = False) -> tuple[str, str]:
    template_file = ROOT / str(config.get("template_file") or "config/templates/research_invitation.txt")
    template = template_file.read_text(encoding="utf-8")
    values = invitation_values(config, company_name, signup_url=signup_url, signup_is_simulated=signup_is_simulated)
    subject = render_template(str(config.get("subject_template") or "University research invitation: {company_name}"), values)
    body = render_template(template, values)
    return subject, body


def build_form_draft(company: dict[str, Any], contact: dict[str, Any], config: dict[str, Any],
                     signup_url: str) -> dict[str, str]:
    subject, body = render_invitation(config, company["company_name"])
    evidence = contact.get("purpose") or ""
    mapping = map_contact_form_fields(evidence)
    proposed = {
        "name": "[NOT CONFIGURED — NOT SUBMITTED]" if "name" in mapping else "",
        "email": "[REPLY-TO NOT CONFIGURED — NOT SUBMITTED]" if "email" in mapping else "",
        "company": company["company_name"] if "company" in mapping else "",
        "subject": subject if "subject" in mapping else "",
        "message": body if "message" in mapping else "",
        "phone": "" if "phone" in mapping else "",
        "consent": "" ,
    }
    safety = form_submission_safety(evidence)
    return {
        "demo_label": "DEMO — NOT SUBMITTED",
        "company_id": str(company["id"]), "company_name": company["company_name"], "contact_id": str(contact["id"]),
        "form_url": contact.get("channel_url") or "", "evidence_url": contact.get("source_url") or "",
        "visible_form_evidence": evidence[:1200], "field_mapping_json": json.dumps(mapping, ensure_ascii=False, sort_keys=True),
        "proposed_values_json": json.dumps(proposed, ensure_ascii=False, sort_keys=True), **safety,
        "company_review_status": company["review_status"], "channel_review_status": contact["review_status"],
    }


def _candidate_row(company: dict[str, Any], contact: dict[str, Any] | None, *, status: str) -> dict[str, str]:
    if contact:
        route = contact.get("email") or contact.get("channel_url") or ""
        return {
            "demo_label": "REAL SAVED EVIDENCE — DEMO ONLY", "company_id": str(company["id"]),
            "company_name": company["company_name"], "website": company["website"],
            "eligibility": company.get("eligibility") or "NEEDS_REVIEW", "geo_opportunity": (company.get("geo_opportunity") or "UNKNOWN").upper(),
            "research_priority": company.get("priority_tier") or "NEEDS_REVIEW",
            "identity_verification_status": company["identity_verification_status"],
            "company_review_status": company["review_status"], "contact_status": company["contact_status"],
            "contact_id": str(contact["id"]), "channel_type": contact["channel_type"], "recipient_or_form_url": route,
            "channel_review_status": contact["review_status"], "channel_validation_status": contact.get("validation_status") or "UNKNOWN",
            "evidence_url": contact.get("source_url") or "", "evidence_excerpt": (contact.get("evidence_text") or contact.get("purpose") or "")[:500],
            "candidate_status": status,
        }
    return {
        "demo_label": "REAL SAVED COMPANY — NO SUITABLE CHANNEL", "company_id": str(company["id"]),
        "company_name": company["company_name"], "website": company["website"],
        "eligibility": company.get("eligibility") or "NEEDS_REVIEW", "geo_opportunity": (company.get("geo_opportunity") or "UNKNOWN").upper(),
        "research_priority": company.get("priority_tier") or "NEEDS_REVIEW",
        "identity_verification_status": company["identity_verification_status"],
        "company_review_status": company["review_status"], "contact_status": company["contact_status"],
        "contact_id": "", "channel_type": "NONE", "recipient_or_form_url": "", "channel_review_status": "NOT_APPLICABLE",
        "channel_validation_status": "NO_SUITABLE_CHANNEL", "evidence_url": company["website"],
        "evidence_excerpt": "No suitable first-party business email or official contact form is stored in the saved cohort evidence.",
        "candidate_status": status,
    }


def run_demo(database: Path = DB_PATH, output_dir: Path = DATA, max_draft_companies: int = 10) -> dict[str, int]:
    if not 10 <= max_draft_companies <= 20:
        raise ValueError("max-draft-companies must be between 10 and 20")
    config = read_yaml(OUTREACH_CONFIG)
    if config.get("live_sending_enabled") is not False:
        raise ValueError("Demo Mode requires live_sending_enabled: false")
    scenario = read_yaml(SCENARIO_PATH)
    companies, contacts = load_local_cohort(database)
    output_dir.mkdir(parents=True, exist_ok=True)
    prior_successful_simulations = {
        row["idempotency_key"]: row
        for row in read_csv(output_dir / "phase5a_delivery_simulation.csv")
        if row.get("delivery_status") == "SIMULATED_DELIVERED"
        and row.get("attempt_status") == "IDEMPOTENT_SIMULATION_EVENT"
        and row.get("idempotency_key")
    }
    selected = select_demo_channels(companies, contacts, max_draft_companies, scenario)
    no_channel_id = int(scenario["scenarios"]["no_channel_company_id"])
    no_channel_company = companies[no_channel_id]
    if no_channel_company["identity_verification_status"] != "VERIFIED" or no_channel_company.get("eligibility") != "ELIGIBLE":
        raise ValueError("configured no-channel scenario must remain an identity-verified eligible company")
    if any(_is_first_party_channel(no_channel_company, contact) for contact in contacts.get(no_channel_id, [])):
        raise ValueError("configured no-channel scenario now has a suitable saved channel; update the demo fixture deliberately")

    template = (ROOT / str(config.get("template_file") or "config/templates/research_invitation.txt")).read_text(encoding="utf-8")
    if "{research_signup_section}" not in template:
        raise ValueError("invitation template must contain the optional {research_signup_section} placeholder")
    signup = scenario["simulated_signup"]
    simulated_signup_url = str(signup["url"])
    if not simulated_signup_url.endswith(".invalid/phase5a-signup"):
        raise ValueError("demo signup fixture must use the reserved .invalid domain")
    simulated_approval = bool(scenario.get("simulated_approval_fixture"))
    suppression_path = ROOT / str(config.get("suppression_file") or "data/outreach_suppression.csv")
    suppression = SuppressionRegistry.load(suppression_path)
    transport = DemoTransport()

    candidates: list[dict[str, str]] = []
    email_drafts: list[dict[str, str]] = []
    form_drafts: list[dict[str, str]] = []
    delivery_rows: list[dict[str, str]] = []
    scenario_email_id = int(scenario["scenarios"]["business_email_company_id"])
    scenario_form_id = int(scenario["scenarios"]["contact_form_company_id"])
    scenario_response = scenario["simulated_response"]

    for company, contact in selected:
        company_id = company["id"]
        candidates.append(_candidate_row(company, contact, status="SUITABLE_CHANNEL_DRAFT_SAMPLE"))
        if contact["channel_type"] == "CONTACT_FORM":
            form_drafts.append(build_form_draft(company, contact, config, simulated_signup_url))
            delivery_rows.append({
                "demo_label": "DEMO — FORM NEVER SUBMITTED", "company_id": str(company_id), "company_name": company["company_name"],
                "contact_id": str(contact["id"]), "delivery_mode": "DEMO", "delivery_status": "NOT_SUBMITTED_DEMO",
                "provider_message_id": "", "idempotency_key": "", "attempt_status": "NO_FORM_SUBMISSION_CODE_PATH",
                "simulated_approval_fixture": "NOT_USED", "real_company_review_status": company["review_status"],
                "real_channel_review_status": contact["review_status"], "real_outreach_approval_status": "NOT_GRANTED",
                "simulated_response_status": "", "simulated_response_text": "", "signup_handoff_status": "NOT_APPLICABLE",
                "signup_url": "", "signup_completed": "false", "research_consent_status": "NOT_ESTABLISHED",
                "outreach_state_trace": "NOT_SUBMITTED_DEMO", "form_submission_status": "NOT_SUBMITTED_DEMO",
            })
            continue

        recipient = validate_recipient(contact.get("email"))
        use_simulated_signup = company_id == scenario_email_id and bool(signup.get("approval_fixture"))
        subject, body = render_invitation(config, company["company_name"],
                                          signup_url=simulated_signup_url if use_simulated_signup else "",
                                          signup_is_simulated=use_simulated_signup)
        key = idempotency_key(company_id, contact["id"], recipient, subject, body)
        email_drafts.append({
            "demo_label": "DEMO DRAFT — NOT A REAL MESSAGE", "company_id": str(company_id),
            "company_name": company["company_name"], "contact_id": str(contact["id"]), "recipient": recipient,
            "sender_name": str(config.get("sender_name") or ""), "sender_email": str(config.get("sender_email") or ""),
            "reply_to_email": str(config.get("reply_to_email") or ""), "subject": subject, "body": body,
            "evidence_url": contact.get("source_url") or "", "evidence_excerpt": (contact.get("evidence_text") or "")[:500],
            "company_review_status": company["review_status"], "channel_review_status": contact["review_status"],
            "outreach_state": "NEEDS_REVIEW", "approval_status": "NOT_APPROVED",
            "signup_url_included": "SIMULATED_ONLY" if use_simulated_signup else "false", "idempotency_key": key,
        })
        candidate_gate = ApprovalGate(
            identity_verified=company["identity_verification_status"] == "VERIFIED",
            eligibility=(company.get("eligibility") or "").upper(), channel_type=contact["channel_type"],
            recipient_valid=True, company_review_status=company["review_status"],
            channel_review_status=contact["review_status"], opted_out=suppression.is_suppressed(recipient),
        )
        gate_status = evaluate_delivery_gate(candidate_gate, demo_fixture_approval=simulated_approval and company_id == scenario_email_id)
        is_scenario_delivery = company_id == scenario_email_id
        if is_scenario_delivery and key in prior_successful_simulations:
            # Reuse the existing demo event row rather than creating a second delivery/reply/handoff.
            delivery_rows.append(prior_successful_simulations[key])
            continue
        result = transport.deliver(
            company_id=company_id, contact_id=contact["id"], recipient=recipient,
            subject=subject, body=body, approval_status=gate_status,
        ) if is_scenario_delivery else None
        simulated_response_state = ""
        response_text = ""
        signup_status = "NOT_APPLICABLE"
        signup_url = ""
        signup_completed = "false"
        state_trace = [OutreachState.NOT_PREPARED]
        state_trace.append(transition(state_trace[-1], OutreachState.DRAFT_READY))
        state_trace.append(transition(state_trace[-1], OutreachState.NEEDS_REVIEW))
        delivery_status = "NOT_SENT_DRAFT_ONLY"
        message_id = ""
        if is_scenario_delivery and result and result.status == "SIMULATED_DELIVERED":
            state_trace.append(transition(state_trace[-1], OutreachState.APPROVED))
            state_trace.append(transition(state_trace[-1], OutreachState.SENDING))
            state_trace.append(transition(state_trace[-1], OutreachState.SENT))
            delivery_status = result.status
            message_id = result.provider_message_id
            state_trace.append(transition(state_trace[-1], OutreachState.RESPONSE_RECEIVED))
            response_text = str(scenario_response["text"])
            response_state = classify_response(str(scenario_response["type"]))
            state_trace.append(transition(state_trace[-1], response_state))
            simulated_response_state = "SIMULATED_" + response_state.value
            handoff = prepare_signup_handoff(
                current_state=response_state, signup_url=simulated_signup_url,
                signup_url_approval_fixture=bool(signup.get("approval_fixture")), demo=True,
            )
            if handoff["status"] == "SIMULATED_HANDOFF_READY":
                state_trace.append(transition(state_trace[-1], OutreachState.HANDOFF_READY))
            signup_status = handoff["status"]
            signup_url = handoff["url"]
        else:
            delivery_status = result.status if result else "NOT_SENT_DRAFT_ONLY"
        delivery_rows.append({
            "demo_label": "SIMULATED — NO NETWORK DELIVERY", "company_id": str(company_id),
            "company_name": company["company_name"], "contact_id": str(contact["id"]), "delivery_mode": "DEMO_SIMULATED",
            "delivery_status": delivery_status, "provider_message_id": message_id,
            "idempotency_key": result.idempotency_key if result else key,
            "attempt_status": "IDEMPOTENT_SIMULATION_EVENT" if is_scenario_delivery else "DRAFT_ONLY",
            "simulated_approval_fixture": "DEMO_FIXTURE_NOT_A_PERSON" if is_scenario_delivery else "NOT_USED",
            "real_company_review_status": company["review_status"], "real_channel_review_status": contact["review_status"],
            "real_outreach_approval_status": "NOT_GRANTED", "simulated_response_status": simulated_response_state,
            "simulated_response_text": response_text, "signup_handoff_status": signup_status,
            "signup_url": signup_url, "signup_completed": signup_completed,
            "research_consent_status": "NOT_ESTABLISHED", "outreach_state_trace": " -> ".join("SIMULATED_" + state.value for state in state_trace),
            "form_submission_status": "NOT_APPLICABLE",
        })

    candidates.append(_candidate_row(no_channel_company, None, status="NO_SUITABLE_CHANNEL — NO DRAFT OR DELIVERY"))
    delivery_rows.append({
        "demo_label": "DEMO — NO USABLE CHANNEL", "company_id": str(no_channel_id),
        "company_name": no_channel_company["company_name"], "contact_id": "", "delivery_mode": "DEMO",
        "delivery_status": "NOT_SENT_NO_CHANNEL", "provider_message_id": "", "idempotency_key": "",
        "attempt_status": "NO_SUCH_CHANNEL", "simulated_approval_fixture": "NOT_USED",
        "real_company_review_status": no_channel_company["review_status"], "real_channel_review_status": "NOT_APPLICABLE",
        "real_outreach_approval_status": "NOT_GRANTED", "simulated_response_status": "",
        "simulated_response_text": "", "signup_handoff_status": "NOT_APPLICABLE", "signup_url": "",
        "signup_completed": "false", "research_consent_status": "NOT_ESTABLISHED",
        "outreach_state_trace": OutreachState.NOT_PREPARED.value, "form_submission_status": "NOT_APPLICABLE",
    })

    write_csv_atomic(output_dir / "phase5a_outreach_candidates.csv", OUTREACH_CANDIDATE_FIELDS, candidates)
    write_csv_atomic(output_dir / "phase5a_email_drafts.csv", EMAIL_DRAFT_FIELDS, email_drafts)
    write_csv_atomic(output_dir / "phase5a_contact_form_drafts.csv", FORM_DRAFT_FIELDS, form_drafts)
    write_csv_atomic(output_dir / "phase5a_delivery_simulation.csv", DELIVERY_FIELDS, delivery_rows)
    result = {
        "cohort_companies": len(companies), "draft_sample_companies": len(selected),
        "email_drafts": len(email_drafts), "contact_form_drafts": len(form_drafts),
        "no_channel_scenarios": 1,
        "simulated_deliveries": sum(row["delivery_status"] == "SIMULATED_DELIVERED" for row in delivery_rows),
        "simulated_responses": sum(row["simulated_response_status"] == "SIMULATED_INTERESTED" for row in delivery_rows),
        "simulated_signup_handoffs": sum(row["signup_handoff_status"] == "SIMULATED_HANDOFF_READY" for row in delivery_rows),
        "real_sends": 0, "real_form_submissions": 0, "database_writes": 0,
    }
    print("PHASE 5A PROFESSOR DEMO — SIMULATED / OFFLINE ONLY")
    print(f"cohort={result['cohort_companies']} draft_companies={result['draft_sample_companies']} email_drafts={result['email_drafts']} form_drafts={result['contact_form_drafts']}")
    print(f"simulated_deliveries={result['simulated_deliveries']} simulated_interested_responses={result['simulated_responses']} simulated_signup_handoffs={result['simulated_signup_handoffs']}")
    print(f"real_sends={result['real_sends']} form_submissions={result['real_form_submissions']} sender_configured={bool(config.get('sender_email'))}")
    for company, contact in selected:
        print(f"  {company['id']:>3}  {company['company_name']}: {contact['channel_type']} — {contact.get('email') or contact.get('channel_url')}")
    print(f"  {no_channel_id:>3}  {no_channel_company['company_name']}: NO_SUITABLE_CHANNEL — no draft")
    return result


def _review_fingerprint(session) -> tuple[dict[int, tuple[Any, ...]], dict[int, tuple[Any, ...]]]:
    company_reviews = {
        row.id: (row.review_status, row.review_notes, row.reviewed_by, row.reviewed_at)
        for row in session.scalars(select(Company).order_by(Company.id)).all()
    }
    contact_reviews = {
        row.id: (row.review_status, row.review_notes, row.reviewed_by, row.reviewed_at)
        for row in session.scalars(select(Contact).order_by(Contact.id)).all()
    }
    return company_reviews, contact_reviews


def enrich_saved_contact_evidence(database: Path = DB_PATH, limit: int = 20, batch_size: int = 25) -> dict[str, int]:
    """Run the existing extractor on saved page evidence only; it never calls the network."""
    if database.name != "phase4f_experimental.db":
        raise ValueError("offline enrichment is restricted to the Phase 4F experimental database")
    if not 1 <= limit <= 100 or not 1 <= batch_size <= 25:
        raise ValueError("limit must be 1..100 and batch size 1..25")
    engine = create_db_engine("sqlite:///" + str(database.resolve()))
    settings = load_settings().website
    company_reviews_before: dict[int, tuple[Any, ...]]
    contact_reviews_before: dict[int, tuple[Any, ...]]
    summaries = []
    try:
        with session_factory(engine)() as session:
            company_reviews_before, contact_reviews_before = _review_fingerprint(session)
            selected: list[int] = []
            companies = session.scalars(select(Company).order_by(Company.priority_score.desc(), Company.id)).all()
            saved_status = stored_identity_statuses()
            for company in companies:
                if company.eligibility != "ELIGIBLE" or company.contact_status.value != "NOT_RUN":
                    continue
                snapshot = session.scalar(select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id).order_by(WebsiteSnapshot.fetched_at.desc(), WebsiteSnapshot.id.desc()).limit(1))
                identity = saved_status.get(company.id) or verify_company_website(company, snapshot).status
                if identity != "VERIFIED":
                    continue
                if any(contact.channel_type and contact.channel_type.value in SUITABLE_CHANNEL_TYPES for contact in company.contacts):
                    continue
                if snapshot is None or snapshot.fetch_status not in {"SUCCESS", "PARTIAL"}:
                    continue
                selected.append(company.id)
                if len(selected) == limit:
                    break
            for offset in range(0, len(selected), batch_size):
                batch = selected[offset:offset + batch_size]
                summary = enrich_contacts(
                    session, priorities=("HIGH", "MEDIUM", "LOW"), company_ids=batch, limit=len(batch),
                    fallback_settings=Crawl4AIFallbackSettings(enabled=False), user_agent=settings.user_agent,
                )
                summaries.append(summary)
                print(f"offline_contact_batch={offset // batch_size + 1} processed={summary.processed} found={summary.found} not_found={summary.not_found} new_contacts={summary.contacts_created} network_requests=0 fallback=disabled")
            company_reviews_after, contact_reviews_after = _review_fingerprint(session)
            if company_reviews_after != company_reviews_before:
                raise RuntimeError("offline enrichment unexpectedly changed company human-review fields")
            for contact_id, review in contact_reviews_before.items():
                if contact_reviews_after.get(contact_id) != review:
                    raise RuntimeError(f"offline enrichment unexpectedly changed contact {contact_id} human-review fields")
    finally:
        engine.dispose()
    return {
        "selected": len(selected), "processed": sum(item.processed for item in summaries),
        "found": sum(item.found for item in summaries), "not_found": sum(item.not_found for item in summaries),
        "contacts_created": sum(item.contacts_created for item in summaries),
        "human_review_fields_changed": 0, "network_requests": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Generate local drafts and simulated workflow outputs from saved cohort evidence.")
    demo.add_argument("--database", type=Path, default=DB_PATH)
    demo.add_argument("--output-dir", type=Path, default=DATA)
    demo.add_argument("--max-draft-companies", type=int, default=10)
    enrich = commands.add_parser("enrich-cache", help="Run existing contact extraction against saved website snapshots only.")
    enrich.add_argument("--database", type=Path, default=DB_PATH)
    enrich.add_argument("--limit", type=int, default=20)
    enrich.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(run_demo(args.database, args.output_dir, args.max_draft_companies), sort_keys=True))
    else:
        print(json.dumps(enrich_saved_contact_evidence(args.database, args.limit, args.batch_size), sort_keys=True))


if __name__ == "__main__":
    main()
