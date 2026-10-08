"""Boundedly crawl, verify, and qualify only Phase 4D additions."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from outreach_agent.config import Crawl4AIFallbackSettings, load_settings
from outreach_agent.contacts import enrich_contacts
from outreach_agent.database import create_db_engine, session_factory
from outreach_agent.models import Company, DiscoveryRecord, WebsiteSnapshot
from outreach_agent.normalization import normalize_domain, normalize_url
from outreach_agent.qualification import qualify_companies
from outreach_agent.website import WebsiteCollector, collect_websites
from outreach_agent.website_identity import verify_company_website
from outreach_agent.wikidata import write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB = DATA / "phase4d_experimental.db"
BASELINE = DATA / "phase3d_company_review_77.csv"
WEBSITE_FIELDS = [
    "company_id", "company_name", "source_names", "source_identifiers", "source_profile_urls",
    "directory_routes", "website_url", "normalized_website_url", "normalized_domain", "verification_status", "verification_reason",
    "identity_tokens", "requested_url", "final_url", "http_status", "fetch_status", "page_title",
    "redirected_to_different_domain", "usable_website_content", "pages_captured", "employee_size_value",
    "employee_size_source",
]
QUALIFICATION_FIELDS = [
    "company_id", "company_name", "source_group", "source_names", "website_url", "normalized_website_url", "normalized_domain",
    "website_verification_status", "eligibility", "geo_opportunity", "research_priority", "priority_score",
    "confidence", "company_size_category", "company_size_value", "company_size_source",
    "website_fetch_status", "usable_website_content", "human_review_status", "contact_status",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--contact-sample-limit", type=int, default=10)
    parser.add_argument("--no-contact-sample", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 25:
        parser.error("batch-size must be between 1 and 25")
    if not 0 <= args.contact_sample_limit <= 10:
        parser.error("contact-sample-limit must be between 0 and 10")

    baseline_domains = {normalize_domain(row["website"]) for row in read_csv(BASELINE)}
    engine = create_db_engine(f"sqlite:///{DB}")
    settings = load_settings().website.model_copy(update={"max_pages_per_company": 2})
    website_results: dict[int, dict] = {}
    verified_ids: list[int] = []
    crawl_totals = Counter()
    try:
        with session_factory(engine)() as session:
            new_companies = session.scalars(
                select(Company).where(~Company.normalized_domain.in_(baseline_domains)).order_by(Company.id)
            ).all()
            if len(new_companies) != 121:
                raise RuntimeError(f"expected 121 Phase 4D additions, found {len(new_companies)}")
            companies_by_id = {company.id: company for company in new_companies}

            # Never request a third-party profile URL. It remains visible as a
            # rejected source URL in the audit rather than a website snapshot.
            crawl_ids = [
                company.id for company in new_companies
                if not verify_company_website(company, None).status == "THIRD_PARTY_URL"
            ]
            collector = WebsiteCollector(settings)
            try:
                for offset in range(0, len(crawl_ids), args.batch_size):
                    batch = crawl_ids[offset:offset + args.batch_size]
                    summary = collect_websites(
                        session, settings, collector=collector, company_ids=batch,
                    )
                    crawl_totals.update({
                        "processed": summary.processed, "fetched": summary.fetched,
                        "cached": summary.cached, "failed": summary.failed, "blocked": summary.blocked,
                    })
                    print(json.dumps({"batch": offset // args.batch_size + 1, "companies": len(batch), "summary": summary.__dict__}, sort_keys=True), flush=True)
            finally:
                collector.close()

            for company in new_companies:
                snapshot = session.scalar(
                    select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id)
                    .order_by(WebsiteSnapshot.fetched_at.desc())
                )
                identity = verify_company_website(company, snapshot)
                if identity.status == "VERIFIED":
                    verified_ids.append(company.id)
                discoveries = company.discoveries
                raw_payloads = []
                for discovery in discoveries:
                    try:
                        raw_payloads.append(json.loads(discovery.raw_data or "{}"))
                    except json.JSONDecodeError:
                        raw_payloads.append({})
                website_results[company.id] = {
                    "company": company, "snapshot": snapshot, "identity": identity,
                    "discoveries": discoveries, "raw_payloads": raw_payloads,
                }

            qualification_summary = qualify_companies(
                session, ROOT / "config/qualification.yaml", force=True, company_ids=verified_ids,
            )

            # Priority is calculated before this bounded contact-channel sample.
            contact_sample = [] if args.no_contact_sample else session.scalars(
                select(Company).where(
                    Company.id.in_(verified_ids or [-1]), Company.priority_tier == "HIGH",
                ).order_by(Company.priority_score.desc(), Company.id).limit(args.contact_sample_limit)
            ).all()
            contact_ids = [company.id for company in contact_sample]
            contact_summary = None
            if contact_ids:
                contact_summary = enrich_contacts(
                    session, priorities=("HIGH",), company_ids=contact_ids,
                    limit=args.contact_sample_limit,
                    fallback_settings=Crawl4AIFallbackSettings(enabled=False),
                    user_agent=settings.user_agent,
                )

            website_rows: list[dict[str, str]] = []
            qualification_rows: list[dict[str, str]] = []
            for company_id in sorted(website_results):
                item = website_results[company_id]
                company, snapshot, identity = item["company"], item["snapshot"], item["identity"]
                pages = snapshot.pages if snapshot else []
                source_names = sorted({discovery.source_name or discovery.source_type for discovery in item["discoveries"]})
                source_ids = sorted({discovery.source_identifier for discovery in item["discoveries"]})
                profile_urls = sorted({discovery.source_url or "" for discovery in item["discoveries"]})
                routes = sorted({str(payload.get("source_directory_url") or "") for payload in item["raw_payloads"] if payload.get("source_directory_url")})
                redirected = bool(snapshot and snapshot.final_url and normalize_domain(snapshot.final_url) != normalize_domain(company.website))
                website_rows.append({
                    "company_id": company.id, "company_name": company.company_name,
                    "source_names": " | ".join(source_names), "source_identifiers": " | ".join(source_ids),
                    "source_profile_urls": " | ".join(profile_urls), "directory_routes": " | ".join(routes),
                    "website_url": company.website, "normalized_domain": company.normalized_domain,
                    "normalized_website_url": normalize_url(company.website),
                    "verification_status": identity.status, "verification_reason": identity.reason,
                    "identity_tokens": identity.identity_tokens,
                    "requested_url": snapshot.requested_url if snapshot else company.website,
                    "final_url": snapshot.final_url if snapshot else "",
                    "http_status": snapshot.http_status if snapshot and snapshot.http_status else "",
                    "fetch_status": snapshot.fetch_status if snapshot else "NOT_FETCHED_THIRD_PARTY",
                    "page_title": snapshot.title if snapshot and snapshot.title else "",
                    "redirected_to_different_domain": str(redirected).lower(),
                    "usable_website_content": str(identity.usable_content).lower(),
                    "pages_captured": sum(page.fetch_status in {"SUCCESS", "PARTIAL"} for page in pages),
                    "employee_size_value": company.company_size_value if company.company_size_value is not None else "",
                    "employee_size_source": company.company_size_source or "unknown",
                })
                qualification_rows.append({
                    "company_id": company.id, "company_name": company.company_name,
                    "source_group": "YC" if any("Y Combinator" in name for name in source_names) else "Greater Dalton Chamber",
                    "source_names": " | ".join(source_names), "website_url": company.website,
                    "normalized_website_url": normalize_url(company.website),
                    "normalized_domain": company.normalized_domain,
                    "website_verification_status": identity.status, "eligibility": company.eligibility or "NEEDS_REVIEW",
                    "geo_opportunity": company.geo_opportunity or "UNKNOWN",
                    "research_priority": company.priority_tier or "NEEDS_REVIEW",
                    "priority_score": company.priority_score if company.priority_score is not None else "",
                    "confidence": company.evidence_confidence or "LOW",
                    "company_size_category": company.company_size_category or "UNKNOWN",
                    "company_size_value": company.company_size_value if company.company_size_value is not None else "",
                    "company_size_source": company.company_size_source or "unknown",
                    "website_fetch_status": snapshot.fetch_status if snapshot else "NOT_FETCHED_THIRD_PARTY",
                    "usable_website_content": str(identity.usable_content).lower(),
                    "human_review_status": company.review_status.value,
                    "contact_status": company.contact_status.value,
                })
            write_csv_atomic(DATA / "phase4e_website_verification.csv", WEBSITE_FIELDS, website_rows)
            write_csv_atomic(DATA / "phase4e_qualification_results.csv", QUALIFICATION_FIELDS, qualification_rows)
            print(json.dumps({
                "new_companies": len(new_companies), "crawled_unique_domains": len(crawl_ids),
                "verified": len(verified_ids), "verified_ids": verified_ids,
                "crawl_totals_across_batches": crawl_totals,
                "qualification": qualification_summary, "contact_sample_company_ids": contact_ids,
                "contact_summary": contact_summary.__dict__ if contact_summary else None,
            }, sort_keys=True, default=list))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
