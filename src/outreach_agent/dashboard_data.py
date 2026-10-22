"""Read-only data services for the local professor dashboard."""

from __future__ import annotations

import csv
import io
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
    mail_merge_safe_subject,
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
from outreach_agent.candidate_pool import candidate_summary
from outreach_agent.phase6e_screening import PHASE6E_DB, phase6e_summary
from outreach_agent.contact_channel_model import classify_contact_channels
from outreach_agent.phase7_smb import PHASE7_DB, phase7_smb_summary
from outreach_agent.geographic_eligibility import classify_geography

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data/phase6a_experimental.db"
DEFAULT_VERIFICATION = ROOT / "data/phase4f_website_verification.csv"
PRIOR_VERIFICATION = ROOT / "data/phase4e_website_verification.csv"
HOTEL_CANDIDATE_DB = ROOT / "data/hotel_candidates.db"
RESEARCH_REGISTRY_DB = ROOT / "data/research_registry.db"


def _hotel_candidate_summary(path: Path = HOTEL_CANDIDATE_DB) -> dict[str, int]:
    """Read the separate hotel-property pipeline without creating or migrating its DB."""
    empty = {"properties": 0, "processed": 0, "verified": 0, "geo_eligible": 0,
             "with_business_email": 0, "with_confirmed_form": 0, "pending_review": 0}
    if not path.exists():
        return empty
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='candidates'").fetchone()
        if not table:
            return empty
        # Use scalar queries so older experimental schemas remain readable.
        properties = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        verified = connection.execute("SELECT COUNT(*) FROM candidates WHERE verification_status='VERIFIED'").fetchone()[0]
        eligible = connection.execute("SELECT COUNT(*) FROM candidates WHERE geo_eligibility_status='ELIGIBLE'").fetchone()[0]
        candidate_columns = {row[1] for row in connection.execute("PRAGMA table_info(candidates)")}
        processed = (connection.execute("SELECT COUNT(*) FROM candidates WHERE last_inspected_at IS NOT NULL").fetchone()[0]
                     if "last_inspected_at" in candidate_columns else 0)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hotel_contact_channels'").fetchone():
            email_count, form_count = connection.execute(
                """SELECT
                   COUNT(DISTINCT CASE WHEN channel_type IN ('GENERAL_BUSINESS_EMAIL','BUSINESS_DEVELOPMENT_EMAIL','PARTNERSHIP_EMAIL','SALES_MARKETING_EMAIL') AND email<>'' THEN candidate_id END),
                   COUNT(DISTINCT CASE WHEN channel_type='CONTACT_FORM' AND channel_url<>'' THEN candidate_id END)
                   FROM hotel_contact_channels WHERE review_status<>'REJECTED'""").fetchone()
        else:
            email_count, form_count = 0, 0
        pending_review = connection.execute("SELECT COUNT(*) FROM candidates WHERE human_review_status='PENDING'").fetchone()[0]
    return {"properties": properties, "processed": processed, "verified": verified,
            "geo_eligible": eligible, "with_business_email": email_count,
            "with_confirmed_form": form_count, "pending_review": pending_review}
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


def _registry_geography(path: Path = RESEARCH_REGISTRY_DB) -> dict[str, str]:
    """Read geographic classifications from the separate registry, fail closed."""
    if not path.exists():
        return {}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(research_records)")}
        if "geographic_classification" not in columns:
            return {}
        grouped: dict[str, set[str]] = {}
        for row in db.execute("SELECT official_website,geographic_classification FROM research_records WHERE profile_type IN ('business_company','hotel_property')"):
            domain = _domain(row[0] or "")
            if domain:
                grouped.setdefault(domain, set()).add(row[1] or "COUNTRY_UNKNOWN")
    return {domain: next(iter(values)) if len(values) == 1 else "COUNTRY_CONFLICT" for domain, values in grouped.items()}


def _registry_us_targets(path: Path = RESEARCH_REGISTRY_DB) -> dict[str, int]:
    if not path.exists():
        return {"companies": 0, "hotels": 0}
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        rows = db.execute("""SELECT profile_type,COUNT(*) FROM research_records
            WHERE profile_type IN ('business_company','hotel_property')
              AND geographic_classification='VERIFIED_US'
              AND identity_verification_status='VERIFIED'
              AND qualification_status='ELIGIBLE'
            GROUP BY profile_type""").fetchall()
    return {"companies": next((row[1] for row in rows if row[0] == "business_company"), 0), "hotels": next((row[1] for row in rows if row[0] == "hotel_property"), 0)}


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
    if db_path.name not in {"phase4f_experimental.db", "phase6a_experimental.db"}:
        raise ValueError("Dashboard is restricted to the isolated Phase 4F/6A experimental database")
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
        geographic_by_domain = _registry_geography()
        for detail in details.values():
            fallback, _ = classify_geography(location=detail.get("location"), profile_type="business_company")
            detail["geographic_classification"] = geographic_by_domain.get(_domain(detail.get("website") or ""), fallback)
        channel_presence = {
            company_id: any(item["is_suitable_first_party"] for item in detail["contact_channels"])
            for company_id, detail in details.items()
        }
        channel_classes = {
            company_id: classify_contact_channels(detail["contact_channels"])
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
            "contact_channel_classes": dict(sorted(Counter(channel_classes.values()).items())),
            "pending_human_reviews": {
                "companies": sum((row.get("review_status") or "PENDING").upper() == "PENDING" for row in companies),
                "contact_channels": sum(
                    (contact.get("review_status") or "PENDING").upper() == "PENDING"
                    and contact["is_suitable_first_party"]
                    for detail in details.values() for contact in detail["contact_channels"]
                ),
            },
            "outreach": _outreach_counts(connection, delivery_path),
            "us_geographic_targets": _registry_us_targets(),
        }
        pool = phase6e_summary() if PHASE6E_DB.exists() else candidate_summary()
        email_companies = sum(
            any(channel["is_suitable_first_party"] and channel.get("channel_type") != "CONTACT_FORM"
                for channel in detail["contact_channels"])
            for detail in details.values()
        )
        ready = sum(
            (row.get("pipeline_status") == "READY_FOR_GEO"
             and row.get("identity_verification_status") == "VERIFIED"
             and _eligibility(row) == "ELIGIBLE")
            for row in companies
        )
        if PHASE6E_DB.exists():
            summary["candidate_pool"] = {
                "total_candidates": pool["candidate_count"],
                "verified_companies": pool["verified"],
                "geo_eligible_companies": pool["geo_eligible"],
                "companies_with_email_contacts": pool["with_email"],
                "outreach_ready_companies": pool["outreach_ready"],
                "new_candidate_records": pool["osm_candidate_count"],
            }
        else:
            summary["candidate_pool"] = {
                "total_candidates": len(companies) + pool["candidate_count"],
                "verified_companies": (summary["website_verification"].get("VERIFIED", 0) + pool["verified"]),
                "geo_eligible_companies": (summary["eligibility"].get("ELIGIBLE", 0) + pool["geo_eligible"]),
                "companies_with_email_contacts": email_companies + pool["with_email"],
                "outreach_ready_companies": ready + pool["outreach_ready"],
                "new_candidate_records": pool["candidate_count"],
            }
        summary["hotel_candidate_pool"] = _hotel_candidate_summary()
        if db_path.resolve() == DEFAULT_DB.resolve() and PHASE7_DB.exists():
            summary["phase7_smb_target"] = phase7_smb_summary()
        filters = {
            "sources": sorted({source for detail in details.values() for source in detail["discovery_sources"]}),
            "website_verification": sorted({row["identity_verification_status"] for row in companies}),
            "eligibility": sorted({_eligibility(row) for row in companies}),
            "geo_opportunity": sorted({(row.get("geo_opportunity") or "UNKNOWN").upper() for row in companies}),
            "research_priority": sorted({(row.get("priority_tier") or "NEEDS_REVIEW").upper() for row in companies}),
            "contact_channel": ["HAS_SUITABLE_CHANNEL", "NO_SUITABLE_CHANNEL"],
        }
    scenario = yaml.safe_load(DEFAULT_SCENARIO.read_text(encoding="utf-8")) or {}
    outreach_config = yaml.safe_load(DEFAULT_OUTREACH_CONFIG.read_text(encoding="utf-8")) or {}
    return {
        "summary": summary,
        "companies": companies,
        "details": details,
        "channel_presence": channel_presence,
        "filters": filters,
        "simulated_signup_url": scenario.get("simulated_signup", {}).get("url", DEMO_PARTICIPATION_FORM_URL),
        "participation_form_configuration": {
            "study_description": outreach_config.get("research_project_description") or "Not configured",
            "privacy_notice": outreach_config.get("participation_form_privacy_notice") or "Not configured",
            "researcher_name": outreach_config.get("researcher_name") or "Not configured",
            "research_team": outreach_config.get("research_team") or "Not configured",
            "university_affiliation": outreach_config.get("university_affiliation") or "Not configured",
            "research_contact_email": outreach_config.get("research_contact_email") or "Not configured",
            "sender_name": outreach_config.get("sender_name") or "Not configured",
            "sender_email": outreach_config.get("sender_email") or "Not configured",
            "reply_to": outreach_config.get("reply_to") or "Not configured",
            "participation_interest_form_url": outreach_config.get("participation_interest_form_url") or "Not configured",
            "delivery_mode": outreach_config.get("delivery_mode") or "TEST",
            "daily_limit": outreach_config.get("daily_limit") or "Not configured",
        },
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
            ("geographic_status", [detail.get("geographic_classification", "COUNTRY_UNKNOWN")]),
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
            "geographic_classification": detail.get("geographic_classification", "COUNTRY_UNKNOWN"),
            "eligibility": _eligibility(company),
            "geo_opportunity": (company.get("geo_opportunity") or "UNKNOWN").upper(),
            "research_priority": (company.get("priority_tier") or "NEEDS_REVIEW").upper(),
            "has_suitable_channel": data["channel_presence"][company_id],
            "company_review_status": (company.get("review_status") or "PENDING").upper(),
        })
    return result


COMPANY_EXPORT_COLUMNS = (
    "Company ID", "Company name", "Website", "Canonical domain", "Industry/category", "Location",
    "Source name", "Source URL", "Identity verification status", "GEO eligibility", "GEO opportunity",
    "Research priority score", "Research priority level", "Qualification confidence",
    "Qualification reason summary", "Contact channel type", "Contact email", "Contact form URL",
    "Contact evidence URL", "Contact review status", "Human review status", "Campaign membership",
    "Outreach status", "Last outreach timestamp", "Response status", "Interest form submission status",
)


def _spreadsheet_safe(value: Any) -> str:
    """Prevent CSV cells from being interpreted as spreadsheet formulas."""
    if value is None:
        return ""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def export_companies_csv(data: dict[str, Any], params: dict[str, str] | None = None,
                         campaign_store: Any | None = None) -> str:
    """Stable one-row-per-company CSV for the complete cohort or current filters.

    Multiple contact channels are represented as deterministic, pipe-separated
    values within their respective columns; company rows are never multiplied.
    Campaign state is read from its isolated sidecar and does not alter cohort data.
    """
    params = params or {}
    rows = filter_companies(data, params)
    rows.sort(key=lambda row: (row["company_name"].casefold(), row["id"]))
    states = campaign_store.company_export_states() if campaign_store else {}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COMPANY_EXPORT_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        company = data["details"][int(row["id"])]
        provenance = sorted(company.get("provenance", []), key=lambda item: (
            str(item.get("source_name") or item.get("source_type") or "").casefold(),
            str(item.get("source_url") or ""), int(item.get("id") or 0),
        ))
        channels = sorted(company.get("contact_channels", []), key=lambda item: (
            str(item.get("channel_type") or ""), str(item.get("email") or "").casefold(),
            str(item.get("channel_url") or ""), int(item.get("id") or 0),
        ))
        state = states.get(int(row["id"]), {})
        values = {
            "Company ID": row["id"], "Company name": company.get("company_name"),
            "Website": company.get("website"), "Canonical domain": company.get("normalized_domain"),
            "Industry/category": company.get("industry"), "Location": company.get("location"),
            "Source name": " | ".join(sorted({str(item.get("source_name") or item.get("source_type") or "") for item in provenance if item.get("source_name") or item.get("source_type")})),
            "Source URL": " | ".join(sorted({str(item.get("source_url")) for item in provenance if item.get("source_url")})),
            "Identity verification status": company.get("identity_verification_status"),
            "GEO eligibility": company.get("eligibility"), "GEO opportunity": company.get("geo_opportunity"),
            "Research priority score": company.get("priority_score"), "Research priority level": company.get("priority_tier"),
            "Qualification confidence": company.get("evidence_confidence"),
            "Qualification reason summary": company.get("qualification_reason"),
            "Contact channel type": " | ".join(sorted({str(item.get("channel_type")) for item in channels if item.get("channel_type")})),
            "Contact email": " | ".join(sorted({str(item.get("email")) for item in channels if item.get("email")})),
            "Contact form URL": " | ".join(sorted({str(item.get("channel_url")) for item in channels if item.get("channel_type") == "CONTACT_FORM" and item.get("channel_url")})),
            "Contact evidence URL": " | ".join(sorted({str(item.get("source_url")) for item in channels if item.get("source_url")})),
            "Contact review status": " | ".join(sorted({str(item.get("review_status")) for item in channels if item.get("review_status")})),
            "Human review status": company.get("review_status"),
            "Campaign membership": state.get("campaign_membership", ""),
            "Outreach status": state.get("outreach_status", "NO_CAMPAIGN_HISTORY"),
            "Last outreach timestamp": state.get("last_outreach_timestamp", ""),
            "Response status": state.get("response_status", ""),
            "Interest form submission status": state.get("interest_form_submission_status", ""),
        }
        writer.writerow({key: _spreadsheet_safe(values.get(key)) for key in COMPANY_EXPORT_COLUMNS})
    return output.getvalue()


def make_draft_preview(data: dict[str, Any], company_id: int, contact_id: int | None) -> dict[str, Any]:
    detail = data["details"].get(company_id)
    if not detail:
        raise KeyError(company_id)
    if detail.get("eligibility", "").upper() != "ELIGIBLE":
        return {"status": "BLOCKED_NOT_ELIGIBLE", "label": "Draft unavailable · not sent", "reason": "Company is not currently machine-classified ELIGIBLE."}
    if detail.get("identity_verification_status") != "VERIFIED":
        return {"status": "BLOCKED_IDENTITY_UNVERIFIED", "label": "Draft unavailable · not sent", "reason": "Website identity is not VERIFIED in saved evidence."}
    channels = [channel for channel in detail["contact_channels"] if channel["is_suitable_first_party"]]
    if not channels:
        return {"status": "NO_SUITABLE_CHANNEL", "label": "Draft unavailable · no suitable channel", "reason": "No suitable first-party email or official contact form is stored. No address is inferred."}
    if detail.get("geographic_classification") != "VERIFIED_US":
        return {"status": "BLOCKED_GEOGRAPHY", "label": "Draft unavailable · US geography not verified", "reason": "Email campaign selection requires VERIFIED_US geographic evidence."}
    selected = next((channel for channel in channels if int(channel["id"]) == contact_id), None)
    if not selected:
        return {"status": "CHANNEL_NOT_FOUND", "label": "Draft unavailable · select saved evidence", "reason": "The selected channel is not a suitable first-party channel for this company."}

    config = yaml.safe_load(DEFAULT_OUTREACH_CONFIG.read_text(encoding="utf-8")) or {}
    template = Path(config.get("template_file") or "config/templates/research_invitation.txt")
    template_path = ROOT / template
    demo_form_url = data.get("simulated_signup_url") or DEMO_PARTICIPATION_FORM_URL
    values = research_invitation_values(config, detail["company_name"], demo_form_url=demo_form_url)
    subject = mail_merge_safe_subject(str(config.get("subject_template") or DEFAULT_RESEARCH_SUBJECT))
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
        "label": "DRAFT · NOT SENT",
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
