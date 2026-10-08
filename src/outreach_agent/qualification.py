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
    eligibility: str = "UNKNOWN"
    geo_opportunity: str = "UNKNOWN"
    geo_reasons: list[str] | None = None
    priority_score: float = 0
    priority_tier: str = "NEEDS_REVIEW"
    confidence: str = "LOW"
    evidence: dict[str, Any] | None = None


def evaluate(signals: dict[str, Any], rules: dict[str, Any]) -> QualificationResult:
    weights = rules["weights"]
    penalties = rules["penalties"]
    score = 0.0
    reasons: list[str] = []
    positive_signals = {
        "has_website": True,
        "customer_facing": signals.get("customer_facing", False),
        "search_dependent": signals.get("search_dependent", False),
        "content_rich": signals.get("content_rich", False),
        "smb_or_startup": str(signals.get("company_size", "")).lower() in {"small", "medium", "smb", "startup"},
        "active": signals.get("active", True),
        "geo_opportunity": str(signals.get("geo_opportunity", "")).lower() in {"medium", "high"},
    }
    for name, present in positive_signals.items():
        if present:
            score += float(weights.get(name, 0))
            reasons.append(f"+{weights.get(name, 0)} {name}")
    for name, penalty in penalties.items():
        if signals.get(name, False):
            score += float(penalty)
            reasons.append(f"{penalty} {name}")
    website = signals.get("website_evidence") or {}
    for name, weight in rules.get("website_weights", {}).items():
        if website.get(name, False):
            score += float(weight)
            reasons.append(f"+{weight} website:{name}")
    for name, penalty in rules.get("website_penalties", {}).items():
        if website.get(name, False):
            score += float(penalty)
            reasons.append(f"{penalty} website:{name}")
    score = max(0.0, min(100.0, score))
    thresholds = rules["thresholds"]
    if score >= float(thresholds["qualified"]):
        decision = CompanyStatus.QUALIFIED
    elif score >= float(thresholds["needs_review"]):
        decision = CompanyStatus.NEEDS_REVIEW
    else:
        decision = CompanyStatus.REJECTED
    website = signals.get("website_evidence") or {}
    fetch_status = signals.get("fetch_status")
    pages = int(website.get("pages_inspected", 0))
    if fetch_status in {"BLOCKED", "ROBOTS_DENIED", "FETCH_FAILED"} or not website:
        confidence = "LOW"
    elif pages >= 2:
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"

    if confidence == "LOW":
        eligibility = "UNKNOWN"
    elif website.get("reachable") and (website.get("meaningful_text") or website.get("substantial_public_information")):
        eligibility = "ELIGIBLE"
    elif website.get("reachable") and signals.get("visible_text_length") is not None and int(signals["visible_text_length"]) < 100:
        eligibility = "LOW_FIT"
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
    if eligibility != "ELIGIBLE":
        geo_opportunity = "UNKNOWN"
    elif website.get("substantial_public_information") and website.get("online_discovery_relevance") and gap_count >= 1:
        geo_opportunity = "HIGH"
    elif website.get("meaningful_text") and gap_count >= 1:
        geo_opportunity = "MEDIUM"
    else:
        geo_opportunity = "LOW"

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
    add("smb_or_startup", str(signals.get("company_size_category", signals.get("company_size", ""))).upper() in {"MICRO", "SMALL", "MEDIUM", "STARTUP"})
    add("online_discovery_relevance", website.get("online_discovery_relevance", False))
    add("geo_opportunity_high", geo_opportunity == "HIGH")
    add("geo_opportunity_medium", geo_opportunity == "MEDIUM")
    add("confidence_high", confidence == "HIGH")
    add("confidence_medium", confidence == "MEDIUM")
    priority = min(100.0, priority)
    if confidence == "LOW" or eligibility in {"UNKNOWN", "NEEDS_REVIEW"}:
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
    if website:
        decision = {
            "ELIGIBLE": CompanyStatus.QUALIFIED,
            "LOW_FIT": CompanyStatus.REJECTED,
            "UNKNOWN": CompanyStatus.NEEDS_REVIEW,
            "NEEDS_REVIEW": CompanyStatus.NEEDS_REVIEW,
        }[eligibility]
    return QualificationResult(score, decision, reasons, eligibility, geo_opportunity, geo_reasons, priority, tier, confidence, structured)


def qualify_companies(session: Session, config_path: Path, force: bool = False) -> dict[str, int]:
    rules = load_yaml(config_path)
    query = select(Company)
    if not force:
        query = query.where(Company.pipeline_status == CompanyStatus.DISCOVERED)
    companies = session.scalars(query.order_by(Company.id)).all()
    counts = {status.value: 0 for status in (CompanyStatus.QUALIFIED, CompanyStatus.NEEDS_REVIEW, CompanyStatus.REJECTED)}
    for company in companies:
        signals: dict[str, Any] = {
            "company_size": company.company_size,
            "company_size_category": company.company_size_category,
            "geo_opportunity": company.geo_opportunity,
        }
        if company.discoveries and company.discoveries[0].raw_data:
            signals.update(json.loads(company.discoveries[0].raw_data))
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
