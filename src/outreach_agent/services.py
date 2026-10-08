from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach_agent.discovery import DiscoverySource
from outreach_agent.models import Company, CompanyStatus, DiscoveryRecord
from outreach_agent.normalization import normalize_company_name, normalize_domain, normalize_url

logger = logging.getLogger(__name__)


@dataclass
class DiscoverySummary:
    processed: int = 0
    created: int = 0
    duplicates: int = 0
    provenance_added: int = 0
    provenance_existing: int = 0


def ingest(session: Session, source: DiscoverySource) -> DiscoverySummary:
    summary = DiscoverySummary()
    for candidate in source.discover():
        summary.processed += 1
        domain = normalize_domain(candidate.website)
        company = session.scalar(select(Company).where(Company.normalized_domain == domain))
        if company is None:
            company = Company(
                company_name=candidate.company_name,
                normalized_name=normalize_company_name(candidate.company_name),
                website=normalize_url(candidate.website),
                normalized_domain=domain,
                industry=candidate.industry,
                location=candidate.location,
                company_size=candidate.company_size,
                geo_opportunity=candidate.geo_opportunity,
                pipeline_status=CompanyStatus.DISCOVERED,
            )
            session.add(company)
            session.flush()
            summary.created += 1
            logger.info("stage=discovery decision=created company_id=%s domain=%s", company.id, domain)
        else:
            summary.duplicates += 1
            logger.info("stage=discovery decision=duplicate company_id=%s domain=%s", company.id, domain)

        identifier = candidate.source_identifier or candidate.source_url or str(source.path.resolve())
        existing = session.scalar(
            select(DiscoveryRecord).where(
                DiscoveryRecord.company_id == company.id,
                DiscoveryRecord.source_type == source.source_type,
                DiscoveryRecord.source_identifier == identifier,
            )
        )
        if existing is None:
            session.add(
                DiscoveryRecord(
                    company_id=company.id,
                    source_type=source.source_type,
                    source_name=candidate.source_name or source.source_type.upper(),
                    source_url=candidate.source_url,
                    source_identifier=identifier,
                    raw_data=json.dumps(candidate.model_dump(mode="json"), sort_keys=True),
                )
            )
            summary.provenance_added += 1
        else:
            summary.provenance_existing += 1
            # Refresh source-provided metadata without creating a new provenance row.
            existing.raw_data = json.dumps(candidate.model_dump(mode="json"), sort_keys=True)
            existing.source_name = candidate.source_name or existing.source_name
            existing.source_url = candidate.source_url or existing.source_url
    session.commit()
    return summary
