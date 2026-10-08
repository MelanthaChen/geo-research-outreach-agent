"""Read-only data services for the local professor dashboard."""

from __future__ import annotations

import csv
import json
import sqlite3
import types
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from outreach_agent.outreach_pipeline import (
    DEFAULT_RESEARCH_SUBJECT,
    DEMO_PARTICIPATION_FORM_URL,
    SUITABLE_CHANNEL_TYPES,
    contact_form_invitation_message,
    form_submission_safety,
    missing_research_configuration,
    map_contact_form_fields,
    research_invitation_values,
    render_template,
    validate_recipient,
)
from outreach_agent.website_identity import verify_company_website

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data/phase4f_experimental.db"
DEFAULT_VERIFICATION = ROOT / "data/phase4f_website_verification.csv"
PRIOR_VERIFICATION = ROOT / "data/phase4e_website_verification.csv"
DEFAULT_DELIVERY = ROOT / "data/phase5a_delivery_simulation.csv"
DEFAULT_OUTREACH_CONFIG = ROOT / "config/outreach.yaml"
DEFAULT_TEMPLATE = ROOT / "config/templates/research_invitation.txt"
DEFAULT_SCENARIO = ROOT / "config/phase5a_demo_scenario.yaml"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _identity_statuses(paths: tuple[Path, ...]) -> dict[int, str]:
    result: dict[int, str] = {}
    for path in paths:
        for row in _read_csv(path):
            if row.get("company_id") and row.get("verification_status"):
                result[int(row["company_id"])] = row["verification_status"].upper()
    return result


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _suitable_channel(company: dict[str, Any], contact: dict[str, Any]) -> bool:
    channel_type = contact.get("channel_type") or ""
    if channel_type not in SUITABLE_CHANNEL_TYPES or contact.get("review_status") == "REJECTED":
        return False
    source_url = contact.get("source_url") or contact.get("channel_url") or ""
    if not source_url or _domain(source_url) != company.get("normalized_domain"):
        return False
    if channel_type == "CONTACT_FORM":
        return bool(contact.get("channel_url")) and _domain(contact["channel_url"]) == company.get("normalized_domain")
    email = (contact.get("email") or "").strip()
    return "@" in email and _domain("https://" + email.rsplit("@", 1)[-1]) == company.get("normalized_domain")


def _domain(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _company_data(connection: sqlite3.Connection, verification_paths: tuple[Path, ...]) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    connection.row_factory = sqlite3.Row
    statuses = _identity_statuses(verification_paths)
    companies: list[dict[str, Any]] = []
    contacts_by_company: dict[int, list[dict[str, Any]]] = {}
    for row in connection.execute("SELECT * FROM companies ORDER BY id"):
        company = _record(row)
        company["identity_verification_status"] = statuses.get(int(company["id"])) or _verify_saved_snapshot(connection, company)
        companies.append(company)
    for row in connection.execute("SELECT * FROM contacts ORDER BY company_id,id"):
        contact = _record(row)
        contacts_by_company.setdefault(int(contact["company_id"]), []).append(contact)
    return companies, contacts_by_company


def _verify_saved_snapshot(connection: sqlite3.Connection, company: dict[str, Any]) -> str:
    snapshot_row = connection.execute(
        "SELECT * FROM website_snapshots WHERE company_id=? ORDER BY fetched_at DESC,id DESC LIMIT 1",
        (company["id"],),
    ).fetchone()
    if snapshot_row is None:
        return verify_company_website(types.SimpleNamespace(**company), None).status
    pages = connection.execute(
        "SELECT * FROM website_pages WHERE snapshot_id=? ORDER BY id", (snapshot_row["id"],)
    ).fetchall()
    snapshot = types.SimpleNamespace(
        **dict(snapshot_row), pages=[types.SimpleNamespace(**dict(page)) for page in pages]
    )
    return verify_company_website(types.SimpleNamespace(**company), snapshot).status


def _source_rows(connection: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for row in connection.execute("SELECT * FROM discovery_records ORDER BY company_id,id"):
        result.setdefault(int(row["company_id"]), []).append(_record(row))
    return result


def _parsed_evidence(value: str | None) -> Any:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [part.strip() for part in value.split(";") if part.strip()]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [{"label": key, "value": item} for key, item in parsed.items()]
    return [str(parsed)] if parsed else []


def _eligibility(company: dict[str, Any]) -> str:
    # Match Phase 4G's established review export: missing eligibility stays visible as NEEDS_REVIEW.
    return (company.get("eligibility") or "NEEDS_REVIEW").upper()


def _company_detail(company: dict[str, Any], contacts: list[dict[str, Any]], provenance: list[dict[str, Any]]) -> dict[str, Any]:
    channels = []
    for contact in contacts:
        item = dict(contact)
        item["is_suitable_first_party"] = _suitable_channel(company, item)
        item["recipient_or_url"] = item.get("email") or item.get("channel_url") or ""
        channels.append(item)
    sources = sorted({row.get("source_name") or row.get("source_type") or "Unknown" for row in provenance})
    missing = []
    if not company.get("qualification_evidence"):
        missing.append("Qualification evidence is not recorded")
    if not company.get("geo_opportunity_reason"):
        missing.append("GEO opportunity rationale is not recorded")
    if not company.get("industry"):
        missing.append("Industry is not recorded")
    if not company.get("location"):
        missing.append("Geography is not recorded")
    if not company.get("company_size_value"):
        missing.append("Employee-size evidence is not recorded")
    if not any(channel["is_suitable_first_party"] for channel in channels):
        missing.append("No suitable first-party email or contact form is stored")
    return {
        **company,
        "eligibility": _eligibility(company),
        "qualification_evidence_items": _parsed_evidence(company.get("qualification_evidence")),
        "qualification_reasons": [part.strip() for part in (company.get("qualification_reason") or "").split(";") if part.strip()],
        "geo_reasons": [part.strip() for part in (company.get("geo_opportunity_reason") or "").split(";") if part.strip()],
        "missing_evidence": missing,
        "provenance": provenance,
        "discovery_sources": sources,
        "contact_channels": channels,
    }


def _outreach_counts(connection: sqlite3.Connection, delivery_path: Path) -> dict[str, int]:
    rows = _read_csv(delivery_path)
    return {
        "real_outreach_records": int(connection.execute("SELECT COUNT(*) FROM outreach").fetchone()[0]),
        "real_sent_records": int(connection.execute("SELECT COUNT(*) FROM outreach WHERE status='SENT'").fetchone()[0]),
        "simulated_delivery_events": sum(row.get("delivery_status") == "SIMULATED_DELIVERED" for row in rows),
        "simulated_response_events": sum(bool(row.get("simulated_response_status")) for row in rows),
        "simulated_handoff_events": sum(row.get("signup_handoff_status") == "SIMULATED_HANDOFF_READY" for row in rows),
    }


def load_dashboard_data(
    db_path: Path = DEFAULT_DB,
    verification_path: Path = DEFAULT_VERIFICATION,
    delivery_path: Path = DEFAULT_DELIVERY,
    prior_verification_path: Path | None = PRIOR_VERIFICATION,
) -> dict[str, Any]:
    """Return a stable cohort snapshot. SQLite is always opened read-only."""
    if db_path.name != "phase4f_experimental.db":
        raise ValueError("Dashboard is restricted to the Phase 4F experimental database")
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        verification_paths = (prior_verification_path, verification_path) if prior_verification_path else (verification_path,)
        companies, contacts = _company_data(connection, tuple(path for path in verification_paths if path is not None))
        provenance = _source_rows(connection)
        details = {
            int(company["id"]): _company_detail(
                company,
                contacts.get(int(company["id"]), []),
                provenance.get(int(company["id"]), []),
            )
            for company in companies
        }
        channel_presence = {
            company_id: any(item["is_suitable_first_party"] for item in detail["contact_channels"])
            for company_id, detail in details.items()
        }
        summary = {
            "total_companies": len(companies),
            "website_verification": dict(sorted(Counter(row["identity_verification_status"] for row in companies).items())),
            "eligibility": dict(sorted(Counter(_eligibility(row) for row in companies).items())),
            "geo_opportunity": dict(sorted(Counter((row.get("geo_opportunity") or "UNKNOWN").upper() for row in companies).items())),
            "research_priority": dict(sorted(Counter((row.get("priority_tier") or "NEEDS_REVIEW").upper() for row in companies).items())),
            "contact_channel_coverage": {
                "with_suitable_first_party_channel": sum(channel_presence.values()),
                "without_suitable_first_party_channel": len(companies) - sum(channel_presence.values()),
            },
            "pending_human_reviews": {
                "companies": sum((row.get("review_status") or "PENDING").upper() == "PENDING" for row in companies),
                "contact_channels": sum(
                    (contact.get("review_status") or "PENDING").upper() == "PENDING"
                    and contact["is_suitable_first_party"]
                    for detail in details.values() for contact in detail["contact_channels"]
                ),
            },
            "outreach": _outreach_counts(connection, delivery_path),
        }
        filters = {
            "sources": sorted({source for detail in details.values() for source in detail["discovery_sources"]}),
            "website_verification": sorted({row["identity_verification_status"] for row in companies}),
            "eligibility": sorted({_eligibility(row) for row in companies}),
            "geo_opportunity": sorted({(row.get("geo_opportunity") or "UNKNOWN").upper() for row in companies}),
            "research_priority": sorted({(row.get("priority_tier") or "NEEDS_REVIEW").upper() for row in companies}),
            "contact_channel": ["HAS_SUITABLE_CHANNEL", "NO_SUITABLE_CHANNEL"],
        }
    scenario = yaml.safe_load(DEFAULT_SCENARIO.read_text(encoding="utf-8")) or {}
    return {
        "summary": summary,
        "companies": companies,
        "details": details,
        "channel_presence": channel_presence,
        "filters": filters,
        "simulated_signup_url": scenario.get("simulated_signup", {}).get("url", DEMO_PARTICIPATION_FORM_URL),
    }


def filter_companies(data: dict[str, Any], params: dict[str, str]) -> list[dict[str, Any]]:
    search = params.get("q", "").casefold().strip()
    result = []
    for company in data["companies"]:
        company_id = int(company["id"])
        detail = data["details"][company_id]
        if search and search not in " ".join((company.get("company_name") or "", company.get("website") or "", company.get("industry") or "", company.get("location") or "")).casefold():
            continue
        checks = (
            ("source", detail["discovery_sources"]),
            ("verification", [company["identity_verification_status"]]),
            ("eligibility", [_eligibility(company)]),
            ("geo", [(company.get("geo_opportunity") or "UNKNOWN").upper()]),
            ("priority", [(company.get("priority_tier") or "NEEDS_REVIEW").upper()]),
        )
        if any(params.get(key) and params[key] not in values for key, values in checks):
            continue
        channel_filter = params.get("channel")
        if channel_filter == "HAS_SUITABLE_CHANNEL" and not data["channel_presence"][company_id]:
            continue
        if channel_filter == "NO_SUITABLE_CHANNEL" and data["channel_presence"][company_id]:
            continue
        result.append({
            "id": company_id,
            "company_name": company["company_name"],
            "website": company["website"],
            "industry": company.get("industry") or "Unknown",
            "location": company.get("location") or "Unknown",
            "discovery_sources": detail["discovery_sources"],
            "identity_verification_status": company["identity_verification_status"],
            "eligibility": _eligibility(company),
            "geo_opportunity": (company.get("geo_opportunity") or "UNKNOWN").upper(),
            "research_priority": (company.get("priority_tier") or "NEEDS_REVIEW").upper(),
            "has_suitable_channel": data["channel_presence"][company_id],
            "company_review_status": (company.get("review_status") or "PENDING").upper(),
        })
    return result


def make_draft_preview(data: dict[str, Any], company_id: int, contact_id: int | None) -> dict[str, Any]:
    detail = data["details"].get(company_id)
    if not detail:
        raise KeyError(company_id)
    if detail.get("eligibility", "").upper() != "ELIGIBLE":
        return {"status": "BLOCKED_NOT_ELIGIBLE", "label": "NOT SENT — draft unavailable", "reason": "Company is not currently machine-classified ELIGIBLE."}
    if detail.get("identity_verification_status") != "VERIFIED":
        return {"status": "BLOCKED_IDENTITY_UNVERIFIED", "label": "NOT SENT — draft unavailable", "reason": "Website identity is not VERIFIED in saved evidence."}
    channels = [channel for channel in detail["contact_channels"] if channel["is_suitable_first_party"]]
    if not channels:
        return {"status": "NO_SUITABLE_CHANNEL", "label": "NOT SENT — no suitable channel", "reason": "No suitable first-party email or official contact form is stored. No address is inferred."}
    selected = next((channel for channel in channels if int(channel["id"]) == contact_id), None)
    if not selected:
        return {"status": "CHANNEL_NOT_FOUND", "label": "NOT SENT — choose saved evidence", "reason": "The selected channel is not a suitable first-party channel for this company."}

    config = yaml.safe_load(DEFAULT_OUTREACH_CONFIG.read_text(encoding="utf-8")) or {}
    template = Path(config.get("template_file") or "config/templates/research_invitation.txt")
    template_path = ROOT / template
    demo_form_url = data.get("simulated_signup_url") or DEMO_PARTICIPATION_FORM_URL
    values = research_invitation_values(config, detail["company_name"], demo_form_url=demo_form_url)
    subject = str(config.get("subject_template") or DEFAULT_RESEARCH_SUBJECT)
    is_form = selected.get("channel_type") == "CONTACT_FORM"
    if is_form:
        body = contact_form_invitation_message(
            detail["company_name"], values["participation_interest_form_url"], values["research_contact_email"]
        )
    else:
        body = render_template(template_path.read_text(encoding="utf-8"), values)
    recipient = ""
    form_mapping: dict[str, str] = {}
    safety: dict[str, str] = {}
    if is_form:
        visible_evidence = selected.get("purpose") or ""
        form_mapping = map_contact_form_fields(visible_evidence)
        safety = form_submission_safety(visible_evidence)
    else:
        recipient = validate_recipient(selected.get("email"))
    return {
        "status": "DRAFT_READY",
        "label": "DEMO DRAFT — NOT SENT",
        "company_id": company_id,
        "company_name": detail["company_name"],
        "contact_id": int(selected["id"]),
        "channel_type": selected.get("channel_type"),
        "recipient": recipient,
        "form_url": selected.get("channel_url") if is_form else "",
        "evidence_url": selected.get("source_url") or "",
        "evidence_excerpt": (selected.get("evidence_text") or selected.get("purpose") or "")[:800],
        "subject": subject,
        "body": body,
        "participation_interest_form_url": values["participation_interest_form_url"],
        "research_contact_email": values["research_contact_email"],
        "missing_configuration": missing_research_configuration(config),
        "form_field_mapping": form_mapping,
        "form_safety": safety,
        "company_review_status": detail.get("review_status", "PENDING"),
        "channel_review_status": selected.get("review_status", "PENDING"),
        "approval_status": "NOT_APPROVED",
        "delivery_status": "NOT_SENT",
    }
