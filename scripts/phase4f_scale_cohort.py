"""Bounded, resumable Phase 4F expansion and processing in an isolated database.

The caller must set OUTREACH_DATABASE_URL to data/phase4f_experimental.db. Discovery
commits each static YC category independently; process batches reuse the normal website
cache and persist every company's snapshot before moving to the next one.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator, TypeVar

from sqlalchemy import select

from outreach_agent.config import load_settings
from outreach_agent.database import create_db_engine, session_factory
from outreach_agent.discovery import GrowthZoneDirectorySource, YCPublicDirectorySource
from outreach_agent.models import Company, ContactabilityStatus, DiscoveryRecord, WebsiteSnapshot
from outreach_agent.normalization import normalize_domain, normalize_url
from outreach_agent.qualification import qualify_companies
from outreach_agent.services import ingest
from outreach_agent.website import WebsiteCollector, collect_websites
from outreach_agent.website_identity import verify_company_website
from outreach_agent.wikidata import write_csv_atomic

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "phase4f_experimental.db"
BASELINE_COHORT = DATA / "phase4d_expanded_company_cohort.csv"
CHECKPOINT = DATA / "phase4f_discovery_checkpoint.json"
YC_CATEGORIES = (
    "b2b", "saas", "fintech", "healthcare", "education", "e-commerce",
    "developer-tools", "artificial-intelligence", "marketing", "enterprise",
    "real-estate", "climate", "biotech", "logistics", "security", "analytics",
    "insurance", "legal-tech", "robotics", "agriculture",
)
CHAMBER_CATEGORY_ROUTES = (
    "https://business.daltonchamber.org/list/Search/technology-telecommunications-electronics-806714",
    "https://business.daltonchamber.org/list/Search/health-care-wellness-human-services-806709",
    "https://business.daltonchamber.org/list/Search/advertising-marketing-media-creative-services-806698",
)
COHORT_FIELDS = [
    "company_id", "company_name", "website_url", "normalized_domain", "is_phase4f_addition",
    "source_names", "source_identifiers", "source_profile_urls", "industry", "location",
    "company_size_category", "company_size_value", "company_size_source", "eligibility",
    "geo_opportunity", "research_priority", "outreach_readiness", "human_review_status", "review_notes", "reviewed_by",
]
WEBSITE_FIELDS = [
    "company_id", "company_name", "source_names", "source_identifiers", "source_profile_urls",
    "directory_routes", "website_url", "normalized_website_url", "normalized_domain",
    "verification_status", "verification_reason", "identity_tokens", "requested_url", "final_url",
    "http_status", "fetch_status", "page_title", "redirected_to_different_domain",
    "usable_website_content", "pages_captured", "employee_size_value", "employee_size_source",
]
QUALIFICATION_FIELDS = [
    "company_id", "company_name", "source_group", "source_names", "website_url",
    "normalized_website_url", "normalized_domain", "website_verification_status", "eligibility",
    "geo_opportunity", "research_priority", "priority_score", "confidence", "company_size_category",
    "company_size_value", "company_size_source", "website_fetch_status", "usable_website_content",
    "human_review_status", "contact_status",
]
T = TypeVar("T")


def chunked(values: Iterable[T], size: int) -> Iterator[list[T]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    batch: list[T] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def baseline_domains() -> set[str]:
    return {normalize_domain(row["website"]) for row in read_csv(DATA / "phase3d_company_review_77.csv")} | {
        normalize_domain(row["website"]) for row in read_csv(BASELINE_COHORT)
    }


def require_isolated_database() -> None:
    configured = os.getenv("OUTREACH_DATABASE_URL", "")
    if not configured.endswith("data/phase4f_experimental.db"):
        raise RuntimeError("set OUTREACH_DATABASE_URL=sqlite:///data/phase4f_experimental.db")
    if not DB_PATH.exists():
        raise RuntimeError("isolated database is missing; create it as a copy of Phase 4D before running")


def write_checkpoint(state: dict) -> None:
    temporary = CHECKPOINT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(CHECKPOINT)


def discover(target: int, category_limit: int, delay: float) -> dict:
    require_isolated_database()
    engine = create_db_engine(os.environ["OUTREACH_DATABASE_URL"])
    started = time.monotonic()
    state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {"completed_categories": [], "categories": {}}
    try:
        with session_factory(engine)() as session:
            existing_profiles = set(session.scalars(
                select(DiscoveryRecord.source_url).where(DiscoveryRecord.source_type == "yc_public", DiscoveryRecord.source_url.is_not(None))
            ).all())
            for slug in YC_CATEGORIES:
                if slug in state["completed_categories"]:
                    continue
                current_count = session.query(Company).count()
                if current_count >= target:
                    break
                route = f"https://www.ycombinator.com/companies/industry/{slug}"
                source = YCPublicDirectorySource(
                    directory_url=route, limit=category_limit, delay_seconds=delay,
                    timeout_seconds=15, seen_profile_urls=existing_profiles,
                )
                before = session.query(Company).count()
                try:
                    summary = ingest(session, source)
                    category_result = summary.__dict__ | {
                        "status": "complete", "route": route,
                        "profile_requests_attempted": source.profile_requests_attempted,
                        "overlapping_profile_links_skipped": source.skipped_seen_profiles,
                    }
                except Exception as exc:
                    session.rollback()
                    category_result = {"status": "failed", "route": route, "error": f"{type(exc).__name__}: {exc}"}
                after = session.query(Company).count()
                category_result.update({"company_count_before": before, "company_count_after": after})
                state["categories"][slug] = category_result
                if category_result["status"] == "complete":
                    state["completed_categories"].append(slug)
                write_checkpoint(state)
                print(json.dumps({"category": slug, **category_result}, sort_keys=True), flush=True)
            state["runtime_seconds"] = round(time.monotonic() - started, 2)
            state["company_count"] = session.query(Company).count()
            state["profile_urls_reused_or_seen"] = len(existing_profiles)
            write_checkpoint(state)
        return state
    finally:
        engine.dispose()


def discover_chamber_categories(limit: int, delay: float) -> dict:
    require_isolated_database()
    engine = create_db_engine(os.environ["OUTREACH_DATABASE_URL"])
    state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {"completed_categories": [], "categories": {}}
    state.setdefault("completed_chamber_routes", [])
    state.setdefault("chamber_categories", {})
    started = time.monotonic()
    try:
        with session_factory(engine)() as session:
            for route in CHAMBER_CATEGORY_ROUTES:
                if route in state["completed_chamber_routes"]:
                    continue
                before = session.query(Company).count()
                source = GrowthZoneDirectorySource(
                    directory_url=route, limit=limit, delay_seconds=delay,
                    timeout_seconds=15, user_agent=load_settings().website.user_agent,
                )
                try:
                    summary = ingest(session, source)
                    result = summary.__dict__ | {"status": "complete", "route": route}
                    state["completed_chamber_routes"].append(route)
                except Exception as exc:
                    session.rollback()
                    result = {"status": "failed", "route": route, "error": f"{type(exc).__name__}: {exc}"}
                result.update({"company_count_before": before, "company_count_after": session.query(Company).count()})
                state["chamber_categories"][route] = result
                write_checkpoint(state)
                print(json.dumps(result, sort_keys=True), flush=True)
            state["chamber_runtime_seconds"] = round(time.monotonic() - started, 2)
            state["company_count"] = session.query(Company).count()
            write_checkpoint(state)
        return state
    finally:
        engine.dispose()


def process(batch_size: int, contact_limit: int, no_contacts: bool) -> dict:
    require_isolated_database()
    engine = create_db_engine(os.environ["OUTREACH_DATABASE_URL"])
    started = time.monotonic()
    base = baseline_domains()
    settings = load_settings().website.model_copy(update={"max_pages_per_company": 2, "max_companies_per_run": batch_size})
    crawl = Counter()
    try:
        with session_factory(engine)() as session:
            added = session.scalars(select(Company).where(~Company.normalized_domain.in_(base)).order_by(Company.id)).all()
            company_ids = [company.id for company in added]
            batches = list(chunked(company_ids, batch_size))
            for batch_number, batch in enumerate(batches, start=1):
                collector = WebsiteCollector(settings)
                try:
                    summary = collect_websites(session, settings, collector=collector, company_ids=batch)
                finally:
                    collector.close()
                crawl.update(summary.__dict__)
                print(json.dumps({"batch": batch_number, "companies": len(batch), "crawl": summary.__dict__}, sort_keys=True), flush=True)

            verified_ids = []
            results = {}
            for company in added:
                snapshot = session.scalar(select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id).order_by(WebsiteSnapshot.fetched_at.desc()))
                identity = verify_company_website(company, snapshot)
                results[company.id] = (company, snapshot, identity)
                if identity.status == "VERIFIED":
                    verified_ids.append(company.id)

            qualified = Counter()
            for batch in chunked(verified_ids, batch_size):
                qualified.update(qualify_companies(
                    session, ROOT / "config/qualification.yaml", force=True,
                    company_ids=batch,
                ))

            contact_ids = []
            contacts = None
            if not no_contacts and contact_limit:
                contact_ids = list(session.scalars(select(Company.id).where(
                    Company.id.in_(verified_ids or [-1]), Company.priority_tier == "HIGH",
                ).order_by(Company.priority_score.desc(), Company.id).limit(contact_limit)).all())
                if contact_ids:
                    from outreach_agent.config import Crawl4AIFallbackSettings
                    from outreach_agent.contacts import enrich_contacts
                    contacts = enrich_contacts(
                        session, priorities=("HIGH",), company_ids=contact_ids, limit=contact_limit,
                        fallback_settings=Crawl4AIFallbackSettings(enabled=False), user_agent=settings.user_agent,
                    )

            website_rows, qualification_rows = [], []
            for company_id, (company, snapshot, identity) in results.items():
                discoveries = company.discoveries
                names = sorted({item.source_name or item.source_type for item in discoveries})
                ids = sorted({item.source_identifier for item in discoveries})
                urls = sorted({item.source_url or "" for item in discoveries})
                payloads = []
                for item in discoveries:
                    try:
                        payloads.append(json.loads(item.raw_data or "{}"))
                    except json.JSONDecodeError:
                        pass
                routes = sorted({str(payload["source_directory_url"]) for payload in payloads if payload.get("source_directory_url")})
                redirected = bool(snapshot and snapshot.final_url and normalize_domain(snapshot.final_url) != company.normalized_domain)
                website_rows.append({
                    "company_id": company.id, "company_name": company.company_name, "source_names": " | ".join(names),
                    "source_identifiers": " | ".join(ids), "source_profile_urls": " | ".join(urls), "directory_routes": " | ".join(routes),
                    "website_url": company.website, "normalized_website_url": normalize_url(company.website), "normalized_domain": company.normalized_domain,
                    "verification_status": identity.status, "verification_reason": identity.reason, "identity_tokens": identity.identity_tokens,
                    "requested_url": snapshot.requested_url if snapshot else company.website, "final_url": snapshot.final_url if snapshot else "",
                    "http_status": snapshot.http_status if snapshot and snapshot.http_status else "", "fetch_status": snapshot.fetch_status if snapshot else "NOT_FETCHED",
                    "page_title": snapshot.title if snapshot and snapshot.title else "", "redirected_to_different_domain": str(redirected).lower(),
                    "usable_website_content": str(identity.usable_content).lower(), "pages_captured": sum(page.fetch_status in {"SUCCESS", "PARTIAL"} for page in snapshot.pages) if snapshot else 0,
                    "employee_size_value": company.company_size_value if company.company_size_value is not None else "", "employee_size_source": company.company_size_source or "unknown",
                })
                source_group = "YC" if any("Y Combinator" in name for name in names) else "Greater Dalton Chamber"
                qualification_rows.append({
                    "company_id": company.id, "company_name": company.company_name, "source_group": source_group, "source_names": " | ".join(names),
                    "website_url": company.website, "normalized_website_url": normalize_url(company.website), "normalized_domain": company.normalized_domain,
                    "website_verification_status": identity.status, "eligibility": company.eligibility or "NEEDS_REVIEW",
                    "geo_opportunity": (company.geo_opportunity or "UNKNOWN").upper(), "research_priority": company.priority_tier or "NEEDS_REVIEW",
                    "priority_score": company.priority_score if company.priority_score is not None else "", "confidence": company.evidence_confidence or "LOW",
                    "company_size_category": company.company_size_category or "UNKNOWN", "company_size_value": company.company_size_value if company.company_size_value is not None else "",
                    "company_size_source": company.company_size_source or "unknown", "website_fetch_status": snapshot.fetch_status if snapshot else "NOT_FETCHED",
                    "usable_website_content": str(identity.usable_content).lower(), "human_review_status": company.review_status.value,
                    "contact_status": company.contact_status.value,
                })

            # Full cohort export is generated from persisted rows, not from source response ordering.
            all_companies = session.scalars(select(Company).order_by(Company.normalized_domain)).all()
            cohort_rows = []
            review_rows = []
            for company in all_companies:
                names = sorted({item.source_name or item.source_type for item in company.discoveries})
                is_new = company.normalized_domain not in base
                cohort_row = {
                    "company_id": company.id, "company_name": company.company_name, "website_url": company.website,
                    "normalized_domain": company.normalized_domain, "is_phase4f_addition": str(is_new).lower(),
                    "source_names": " | ".join(names), "source_identifiers": " | ".join(sorted({item.source_identifier for item in company.discoveries})),
                    "source_profile_urls": " | ".join(sorted({item.source_url or "" for item in company.discoveries})),
                    "industry": company.industry or "", "location": company.location or "",
                    "company_size_category": company.company_size_category or "UNKNOWN", "company_size_value": company.company_size_value if company.company_size_value is not None else "",
                    "company_size_source": company.company_size_source or "unknown", "eligibility": company.eligibility or "NEEDS_REVIEW",
                    "geo_opportunity": (company.geo_opportunity or "UNKNOWN").upper(), "research_priority": company.priority_tier or "NEEDS_REVIEW",
                    "outreach_readiness": {
                        ContactabilityStatus.READY_FOR_REVIEW: "CHANNEL_AVAILABLE",
                        ContactabilityStatus.NEEDS_REVIEW: "NEEDS_CHANNEL_REVIEW",
                        ContactabilityStatus.NO_SUITABLE_CHANNEL: "NO_SUITABLE_CHANNEL",
                        ContactabilityStatus.FETCH_FAILED: "FETCH_FAILED",
                    }.get(company.contactability_status, "NO_SUITABLE_CHANNEL"),
                    "human_review_status": company.review_status.value,
                    "review_notes": company.review_notes or "", "reviewed_by": company.reviewed_by or "",
                }
                cohort_rows.append(cohort_row)
                if is_new and company.review_status.value == "PENDING":
                    review_rows.append(cohort_row | {"website_verification_status": results[company.id][2].status if company.id in results else "PENDING_PROCESSING"})

            write_csv_atomic(DATA / "phase4f_expanded_cohort.csv", COHORT_FIELDS, cohort_rows)
            write_csv_atomic(DATA / "phase4f_website_verification.csv", WEBSITE_FIELDS, website_rows)
            write_csv_atomic(DATA / "phase4f_qualification_results.csv", QUALIFICATION_FIELDS, qualification_rows)
            review_fields = COHORT_FIELDS + ["website_verification_status"]
            write_csv_atomic(DATA / "phase4f_review_queue.csv", review_fields, review_rows)
            metrics = {
                "total_companies": len(all_companies), "new_additions": len(added), "verified_new": len(verified_ids),
                "qualification": dict(qualified), "crawl": dict(crawl),
                "contact_company_ids": contact_ids, "contact_summary": contacts.__dict__ if contacts else None,
                "runtime_seconds": round(time.monotonic() - started, 2),
            }
            (DATA / "phase4f_processing_metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
            print(json.dumps(metrics, sort_keys=True, default=list), flush=True)
            return metrics
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--target", type=int, default=500)
    d.add_argument("--category-limit", type=int, default=50)
    d.add_argument("--delay-seconds", type=float, default=1.0)
    c = sub.add_parser("discover-chamber")
    c.add_argument("--category-limit", type=int, default=60)
    c.add_argument("--delay-seconds", type=float, default=1.0)
    p = sub.add_parser("process")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--contact-limit", type=int, default=10)
    p.add_argument("--no-contacts", action="store_true")
    args = parser.parse_args()
    if args.command == "discover":
        if not 1 <= args.category_limit <= 50:
            parser.error("category-limit must be between 1 and 50")
        print(json.dumps(discover(args.target, args.category_limit, args.delay_seconds), sort_keys=True), flush=True)
    elif args.command == "discover-chamber":
        if not 1 <= args.category_limit <= 60:
            parser.error("category-limit must be between 1 and 60")
        print(json.dumps(discover_chamber_categories(args.category_limit, args.delay_seconds), sort_keys=True), flush=True)
    else:
        if not 1 <= args.batch_size <= 25 or not 0 <= args.contact_limit <= 10:
            parser.error("batch-size must be 1..25 and contact-limit must be 0..10")
        process(args.batch_size, args.contact_limit, args.no_contacts)


if __name__ == "__main__":
    main()
