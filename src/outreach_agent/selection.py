"""Unified, explainable company-selection records and spreadsheet exports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach_agent.config import load_yaml
from outreach_agent.models import BusinessChannelType, Company, Contact, ContactabilityStatus, WebsiteSnapshot
from outreach_agent.qualification import evaluate


REVIEW_FIELDS = [
    "company_id", "company_name", "website", "discovery_source", "industry", "company_size",
    "geography", "eligibility", "eligibility_reasons", "geo_opportunity", "geo_opportunity_reasons", "research_priority",
    "priority_score", "priority_reasons", "evidence_confidence", "website_fetch_status",
    "primary_channel_type", "primary_channel", "channel_evidence_url", "alternative_channels",
    "named_decision_maker", "outreach_readiness", "company_review_status", "channel_review_status",
    "reviewer_notes", "channel_review_notes", "channel_override_contact_id", "channel_override_reason", "missing_information",
]
SUITABLE_CHANNEL_TYPES = {
    BusinessChannelType.PARTNERSHIP_EMAIL, BusinessChannelType.BUSINESS_DEVELOPMENT_EMAIL,
    BusinessChannelType.GENERAL_BUSINESS_EMAIL, BusinessChannelType.NAMED_PERSON_EMAIL,
    BusinessChannelType.CONTACT_FORM, BusinessChannelType.SALES_MARKETING_EMAIL,
}


def _latest_snapshot(session: Session, company_id: int) -> WebsiteSnapshot | None:
    return session.scalar(
        select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company_id)
        .order_by(WebsiteSnapshot.fetched_at.desc(), WebsiteSnapshot.id.desc())
    )


def company_selection_record(company: Company, session: Session, rules: dict[str, Any]) -> dict[str, Any]:
    snapshot = _latest_snapshot(session, company.id)
    discoveries = sorted(company.discoveries, key=lambda row: row.id)
    signals: dict[str, Any] = {
        "company_size": company.company_size,
        "company_size_category": company.company_size_category,
        "discovery_source": bool(discoveries),
    }
    if discoveries and discoveries[0].raw_data:
        source_data = json.loads(discoveries[0].raw_data)
        for key in ("active", "ineligible", "unsuitable"):
            if key in source_data:
                signals[key] = source_data[key]
    evidence: dict[str, Any] = {}
    if snapshot:
        evidence = json.loads(snapshot.extracted_signals or "{}")
        evidence["fetch_failed"] = snapshot.fetch_status in {"FETCH_FAILED", "ROBOTS_DENIED"}
        evidence["blocked"] = snapshot.fetch_status == "BLOCKED"
        signals.update({
            "website_evidence": evidence,
            "fetch_status": snapshot.fetch_status,
            "visible_text_length": snapshot.visible_text_length,
        })
    result = evaluate(signals, rules)

    channel = None
    if company.channel_override_contact_id is not None:
        override = session.get(Contact, company.channel_override_contact_id)
        if override and override.company_id == company.id:
            channel = override
    elif company.primary_channel_id is not None:
        channel = session.get(Contact, company.primary_channel_id)
    channels = session.scalars(
        select(Contact).where(Contact.company_id == company.id, Contact.channel_type.is_not(None))
        .order_by(Contact.recommended.desc(), Contact.ranking_score.desc(), Contact.id)
    ).all()
    alternatives = [item for item in channels if channel is None or item.id != channel.id]
    people = [item.name for item in company.contacts if item.contact_type.value == "PERSON" and item.name]
    status = company.contactability_status
    readiness = {
        ContactabilityStatus.READY_FOR_REVIEW: "CHANNEL_AVAILABLE",
        ContactabilityStatus.NEEDS_REVIEW: "NEEDS_CHANNEL_REVIEW",
        ContactabilityStatus.NO_SUITABLE_CHANNEL: "NO_SUITABLE_CHANNEL",
        ContactabilityStatus.FETCH_FAILED: "FETCH_FAILED",
    }.get(status, "NO_SUITABLE_CHANNEL")
    if company.channel_override_contact_id is not None:
        # An explicit override is a human-selected route awaiting channel review.
        readiness = "NEEDS_CHANNEL_REVIEW" if channel and channel.channel_type in SUITABLE_CHANNEL_TYPES else "NO_SUITABLE_CHANNEL"
    missing: list[str] = []
    size_value = None
    if company.company_size_value is not None and company.company_size_value > 0:
        size_value = f"{company.company_size_value} employees (directory-reported)"
    elif company.company_size_value == 0:
        missing.append("company_size_count_zero_needs_verification")
    if not company.industry:
        missing.append("industry")
    if not company.location:
        missing.append("geography")
    if not size_value and company.company_size_value != 0:
        missing.append("company_size")
    if snapshot is None or result.confidence == "LOW":
        missing.append("website_evidence")
    if result.eligibility == "NEEDS_REVIEW":
        missing.append("eligibility_evidence")
    if signals.get("active") is False:
        missing.append("discovery_activity_status_conflicts_with_current_public_website")
    if signals.get("active") is False:
        missing.append("discovery_activity_status_conflicts_with_current_public_website")
    if result.geo_opportunity == "UNKNOWN":
        missing.append("GEO_opportunity_evidence")
    eligibility_reasons = []
    if result.eligibility == "ELIGIBLE":
        eligibility_reasons = ["discovery provenance is recorded", "first-party website is reachable and has meaningful public content"]
    elif result.eligibility == "INELIGIBLE":
        eligibility_reasons = ["explicit configured exclusion evidence: " + ", ".join(rules.get("eligibility", {}).get("explicit_exclusions", []))]
    elif signals.get("active") is False:
        eligibility_reasons = ["discovery source explicitly marked the company inactive; current activity requires review"]
    elif signals.get("active") is False:
        eligibility_reasons = ["discovery source explicitly marked the company inactive; current activity requires review"]
    else:
        eligibility_reasons = ["insufficient evidence to establish an eligible research candidate; no exclusion inferred"]
    return {
        "company_id": company.id,
        "company_name": company.company_name,
        "website": company.website,
        "discovery_source": "; ".join(sorted({d.source_name or d.source_type for d in discoveries})),
        "industry": company.industry or "",
        "company_size": size_value or "",
        "geography": company.location or "",
        "eligibility": result.eligibility,
        "eligibility_reasons": "; ".join(eligibility_reasons),
        "geo_opportunity": result.geo_opportunity,
        "geo_opportunity_reasons": "; ".join(result.geo_reasons or []),
        "research_priority": result.priority_tier,
        "priority_score": f"{result.priority_score:.1f}",
        "priority_reasons": "; ".join(result.evidence.get("priority_math", []) if result.evidence else []),
        "evidence_confidence": result.confidence,
        "website_fetch_status": snapshot.fetch_status if snapshot else "NOT_INSPECTED",
        "primary_channel_type": channel.channel_type.value if channel and channel.channel_type else "",
        "primary_channel": (channel.email or channel.channel_url or channel.source_url or "") if channel else "",
        "channel_evidence_url": channel.source_url if channel else "",
        "alternative_channels": json.dumps([
            {"type": item.channel_type.value if item.channel_type else "", "value": item.email or item.channel_url or item.source_url,
             "evidence_url": item.source_url, "review_status": item.review_status.value}
            for item in alternatives
        ], ensure_ascii=False, sort_keys=True),
        "named_decision_maker": "; ".join(sorted(set(people))),
        "outreach_readiness": readiness,
        "company_review_status": company.review_status.value,
        "channel_review_status": channel.review_status.value if channel else "",
        "reviewer_notes": company.review_notes or "",
        "channel_review_notes": channel.review_notes or "" if channel else "",
        "channel_override_contact_id": company.channel_override_contact_id or "",
        "channel_override_reason": company.channel_override_reason or "",
        "missing_information": "; ".join(missing),
        "_priority_score": result.priority_score,
        "_eligibility": result.eligibility,
        "_geo": result.geo_opportunity,
        "_confidence": result.confidence,
        "_readiness": readiness,
        "_has_channel": bool(channel and channel.channel_type in SUITABLE_CHANNEL_TYPES),
        "_evidence": evidence,
    }


def export_company_selection(session: Session, rules_path: Path, output: Path, summary_output: Path | None = None) -> list[dict[str, Any]]:
    rules = load_yaml(rules_path)
    companies = session.scalars(select(Company).order_by(Company.id)).unique().all()
    records = [company_selection_record(company, session, rules) for company in companies]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record[key] for key in REVIEW_FIELDS})
    if summary_output is not None:
        summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary: list[tuple[str, int, str]] = []
        for label in ("ELIGIBLE", "INELIGIBLE", "NEEDS_REVIEW"):
            summary.append((f"eligibility_{label.lower()}", sum(row["_eligibility"] == label for row in records), ""))
        for label in ("HIGH", "MEDIUM", "LOW", "NEEDS_REVIEW"):
            summary.append((f"priority_{label.lower()}", sum(row["research_priority"] == label for row in records), ""))
        for label in ("HIGH", "MEDIUM", "LOW"):
            summary.append((f"evidence_confidence_{label.lower()}", sum(row["evidence_confidence"] == label for row in records), ""))
        for label in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"):
            summary.append((f"geo_opportunity_{label.lower()}", sum(row["_geo"] == label for row in records), ""))
        for label in ("CHANNEL_AVAILABLE", "NEEDS_CHANNEL_REVIEW", "NO_SUITABLE_CHANNEL", "FETCH_FAILED"):
            summary.append((f"outreach_{label.lower()}", sum(row["_readiness"] == label for row in records), ""))
        summary.append(("companies_with_channel_available", sum(row["_readiness"] == "CHANNEL_AVAILABLE" for row in records), ""))
        summary.append(("companies_needing_channel_review", sum(row["_readiness"] == "NEEDS_CHANNEL_REVIEW" for row in records), ""))
        high_with = [row for row in records if row["research_priority"] == "HIGH" and row["_has_channel"]]
        high_without = [row for row in records if row["research_priority"] == "HIGH" and not row["_has_channel"]]
        ready = [row for row in records if row["_eligibility"] == "ELIGIBLE" and row["_has_channel"]]
        blocked = [row for row in records if row["_eligibility"] == "NEEDS_REVIEW" or row["_confidence"] == "LOW"]
        summary.extend([
            ("missing_company_size", sum("company_size" in row["missing_information"].split("; ") for row in records), "Employee/company-size evidence not available"),
            ("zero_employee_count_needs_verification", sum("company_size_count_zero_needs_verification" in row["missing_information"] for row in records), "Directory reported zero; treated as unverified, not a size exclusion"),
            ("missing_industry", sum("industry" in row["missing_information"].split("; ") for row in records), ""),
            ("missing_geography", sum("geography" in row["missing_information"].split("; ") for row in records), ""),
            ("companies_with_suitable_channels", sum(row["_has_channel"] for row in records), ""),
            ("high_priority_with_suitable_channels", len(high_with), "; ".join(row["company_name"] for row in high_with)),
            ("high_priority_without_suitable_channels", len(high_without), "; ".join(row["company_name"] for row in high_without)),
            ("companies_ready_for_human_review", len(ready), "ELIGIBLE with a suitable route identified; pending company/channel approvals remain independent"),
            ("companies_blocked_by_missing_evidence", len(blocked), "; ".join(row["company_name"] for row in blocked)),
            ("total_companies", len(records), ""),
        ])
        with summary_output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["metric", "count", "details"])
            writer.writerows(summary)
    return records
