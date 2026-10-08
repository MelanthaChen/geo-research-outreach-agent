from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach_agent.config import load_yaml
from outreach_agent.models import Company, CompanyStatus, WebsiteSnapshot, transition_company

logger = logging.getLogger(__name__)


@dataclass
class QualificationResult:
    score: float
    decision: CompanyStatus
    reasons: list[str]
    eligibility: str = "NEEDS_REVIEW"
    geo_opportunity: str = "UNKNOWN"
    geo_reasons: list[str] | None = None
    priority_score: float = 0
    priority_tier: str = "NEEDS_REVIEW"
    confidence: str = "LOW"
    evidence: dict[str, Any] | None = None


def evaluate(signals: dict[str, Any], rules: dict[str, Any]) -> QualificationResult:
    website = signals.get("website_evidence") or {}
    fetch_status = signals.get("fetch_status")
    pages = int(website.get("pages_inspected", 0))
    if fetch_status in {"BLOCKED", "ROBOTS_DENIED", "FETCH_FAILED"} or not website:
        confidence = "LOW"
    elif pages >= 2:
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"

    # Eligibility is deliberately independent of industry, geography, size, and
    # contactability. Only explicit exclusion evidence can make a record ineligible.
    exclusions = rules.get("eligibility", {}).get("explicit_exclusions", [])
    explicit_exclusion = next((key for key in exclusions if signals.get(key) is True), None)
    discovery_supported = bool(signals.get("discovery_source"))
    if explicit_exclusion:
        eligibility = "INELIGIBLE"
    elif signals.get("active") is False:
        eligibility = "NEEDS_REVIEW"
    elif website.get("reachable") and (website.get("meaningful_text") or website.get("substantial_public_information")) and discovery_supported:
        eligibility = "ELIGIBLE"
    else:
        eligibility = "NEEDS_REVIEW"

    geo_reasons: list[str] = []
    gap_count = 0
    if website.get("substantial_public_information"):
        geo_reasons.append("substantial public information can support answer-oriented optimization")
    if website.get("online_discovery_relevance"):
        geo_reasons.append("site structure and content indicate online-discovery relevance")
    if not website.get("faq_page"):
        gap_count += 1
        geo_reasons.append("no FAQ page was observed in the bounded inspection")
    if not website.get("structured_data"):
        gap_count += 1
        geo_reasons.append("no structured data was observed in inspected pages")
    if confidence == "LOW" or not website:
        geo_opportunity = "UNKNOWN"
    elif website.get("substantial_public_information") and website.get("online_discovery_relevance") and gap_count >= 1:
        geo_opportunity = "HIGH"
    elif website.get("meaningful_text") and website.get("online_discovery_relevance"):
        geo_opportunity = "MEDIUM"
    elif website.get("meaningful_text"):
        geo_opportunity = "LOW"
    else:
        geo_opportunity = "UNKNOWN"

    priority_rules = rules.get("priority", {})
    priority = 0.0
    priority_evidence: list[str] = []
    def add(name: str, condition: bool) -> None:
        nonlocal priority
        if condition:
            points = float(priority_rules.get(name, 0))
            priority += points
            priority_evidence.append(f"+{points:g} {name}")

    add("eligible", eligibility == "ELIGIBLE")
    add("analyzable_website", confidence in {"HIGH", "MEDIUM"} and website.get("reachable", False))
    # Company size is descriptive only unless the study explicitly configures
    # size-based selection criteria. Unknown size never changes eligibility/score.
    add("online_discovery_relevance", website.get("online_discovery_relevance", False))
    add("geo_opportunity_high", geo_opportunity == "HIGH")
    add("geo_opportunity_medium", geo_opportunity == "MEDIUM")
    add("confidence_high", confidence == "HIGH")
    add("confidence_medium", confidence == "MEDIUM")
    priority = min(100.0, priority)
    # Keep the legacy qualification_score column as a compatibility alias for
    # the one machine priority score; do not retain a competing heuristic score.
    score = priority
    reasons = list(priority_evidence)
    if confidence == "LOW" or eligibility == "NEEDS_REVIEW":
        tier = "NEEDS_REVIEW"
    elif priority >= float(priority_rules.get("thresholds", {}).get("high", 75)):
        tier = "HIGH"
    elif priority >= float(priority_rules.get("thresholds", {}).get("medium", 50)):
        tier = "MEDIUM"
    else:
        tier = "LOW"
    structured = {
        "positive": [reason for reason in reasons if reason.startswith("+")],
        "negative": [reason for reason in reasons if reason.startswith("-")],
        "opportunity": geo_reasons,
        "uncertainty": (["website evidence unavailable or blocked"] if confidence == "LOW" else []),
        "priority_math": priority_evidence,
    }
    decision = {
        "ELIGIBLE": CompanyStatus.QUALIFIED,
        "INELIGIBLE": CompanyStatus.REJECTED,
        "NEEDS_REVIEW": CompanyStatus.NEEDS_REVIEW,
    }[eligibility]
    return QualificationResult(score, decision, reasons, eligibility, geo_opportunity, geo_reasons, priority, tier, confidence, structured)


def qualify_companies(
    session: Session, config_path: Path, force: bool = False, *, company_ids: list[int] | None = None,
) -> dict[str, int]:
    rules = load_yaml(config_path)
    query = select(Company)
    if not force:
        query = query.where(Company.pipeline_status == CompanyStatus.DISCOVERED)
    if company_ids is not None:
        if not company_ids:
            return {status.value: 0 for status in (CompanyStatus.QUALIFIED, CompanyStatus.NEEDS_REVIEW, CompanyStatus.REJECTED)} | {"processed": 0}
        query = query.where(Company.id.in_(company_ids))
    companies = session.scalars(query.order_by(Company.id)).all()
    counts = {status.value: 0 for status in (CompanyStatus.QUALIFIED, CompanyStatus.NEEDS_REVIEW, CompanyStatus.REJECTED)}
    for company in companies:
        signals: dict[str, Any] = {
            "company_size": company.company_size,
            "company_size_category": company.company_size_category,
            "discovery_source": bool(company.discoveries),
        }
        if company.discoveries and company.discoveries[0].raw_data:
            raw_discovery = json.loads(company.discoveries[0].raw_data)
            # Candidate metadata may supply explicit activity/exclusion evidence,
            # but missing fields are not synthesized.
            signals.update({key: raw_discovery[key] for key in ("active", "ineligible", "unsuitable") if key in raw_discovery})
        snapshot = session.scalar(
            select(WebsiteSnapshot)
            .where(WebsiteSnapshot.company_id == company.id)
            .order_by(WebsiteSnapshot.fetched_at.desc())
        )
        if snapshot is not None:
            evidence = json.loads(snapshot.extracted_signals or "{}")
            evidence["fetch_failed"] = snapshot.fetch_status in {"FETCH_FAILED", "ROBOTS_DENIED"}
            evidence["blocked"] = snapshot.fetch_status == "BLOCKED"
            signals["website_evidence"] = evidence
            signals["fetch_status"] = snapshot.fetch_status
            signals["visible_text_length"] = snapshot.visible_text_length
        result = evaluate(signals, rules)
        company.qualification_score = result.score
        company.qualification_reason = "; ".join(result.reasons)
        company.eligibility = result.eligibility
        company.geo_opportunity = result.geo_opportunity
        company.geo_opportunity_reason = "; ".join(result.geo_reasons or [])
        company.priority_score = result.priority_score
        company.priority_tier = result.priority_tier
        company.evidence_confidence = result.confidence
        company.qualification_evidence = json.dumps(result.evidence, sort_keys=True)
        transition_company(company, result.decision)
        counts[result.decision.value] += 1
        logger.info(
            "stage=qualification decision=%s company_id=%s score=%s reasons=%s",
            result.decision.value,
            company.id,
            result.score,
            company.qualification_reason,
        )
    session.commit()
    counts["processed"] = len(companies)
    return counts
