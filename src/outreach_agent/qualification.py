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
    return QualificationResult(score, decision, reasons)


def qualify_companies(session: Session, config_path: Path, force: bool = False) -> dict[str, int]:
    rules = load_yaml(config_path)
    query = select(Company)
    if not force:
        query = query.where(Company.pipeline_status == CompanyStatus.DISCOVERED)
    companies = session.scalars(query.order_by(Company.id)).all()
    counts = {status.value: 0 for status in (CompanyStatus.QUALIFIED, CompanyStatus.NEEDS_REVIEW, CompanyStatus.REJECTED)}
    for company in companies:
        signals: dict[str, Any] = {"company_size": company.company_size, "geo_opportunity": company.geo_opportunity}
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
        result = evaluate(signals, rules)
        company.qualification_score = result.score
        company.qualification_reason = "; ".join(result.reasons)
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
