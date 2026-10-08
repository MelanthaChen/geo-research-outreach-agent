from __future__ import annotations

from contextlib import contextmanager
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from outreach_agent.config import load_settings
from outreach_agent.contacts import enrich_contacts
from outreach_agent.database import create_db_engine, init_database, session_factory
from outreach_agent.discovery import GrowthZoneDirectorySource, YCPublicDirectorySource, source_for
from outreach_agent.logging_config import configure_logging
from outreach_agent.models import Company, Contact, DiscoveryRecord, ReviewStatus, WebsiteSnapshot
from outreach_agent.qualification import qualify_companies
from outreach_agent.services import ingest
from outreach_agent.website import collect_websites

app = typer.Typer(help="GEO research outreach foundation (no email sending).")
companies_app = typer.Typer(help="Inspect discovered companies.")
review_app = typer.Typer(help="Independent human review workflow.")
candidates_app = typer.Typer(help="Rank research-partner candidates.")
contacts_app = typer.Typer(help="Inspect and independently review public contact evidence.")
app.add_typer(companies_app, name="companies")
app.add_typer(review_app, name="review")
app.add_typer(candidates_app, name="candidates")
app.add_typer(contacts_app, name="contacts")


def _csv_values(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


@contextmanager
def db_session() -> Iterator[Session]:
    settings = load_settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings.database_url)
    init_database(engine)
    with session_factory(engine)() as session:
        yield session


@app.command()
def init_db() -> None:
    """Create missing database tables without deleting existing data."""
    with db_session():
        typer.echo("Database initialized.")


@app.command()
def discover(source: Path = typer.Option(..., exists=True, dir_okay=False, help="CSV or JSON input file.")) -> None:
    """Discover companies from a local file; safe to repeat."""
    with db_session() as session:
        summary = ingest(session, source_for(source))
    typer.echo(
        f"processed={summary.processed} created={summary.created} duplicates={summary.duplicates} "
        f"provenance_added={summary.provenance_added} provenance_existing={summary.provenance_existing}"
    )


@app.command("discover-real")
def discover_real(
    limit: int = typer.Option(25, min=1, max=50, help="Hard cap on companies to discover."),
    directory_url: str = typer.Option(
        "https://www.ycombinator.com/companies/industry/consumer",
        help="Robots-allowed YC static industry directory URL.",
    ),
) -> None:
    """Discover a small sample from YC's public startup directory."""
    settings = load_settings()
    source = YCPublicDirectorySource(
        directory_url=directory_url,
        limit=limit,
        delay_seconds=settings.website.delay_between_requests_seconds,
        timeout_seconds=settings.website.request_timeout_seconds,
        user_agent=settings.website.user_agent,
    )
    with db_session() as session:
        summary = ingest(session, source)
    typer.echo(
        f"processed={summary.processed} created={summary.created} duplicates={summary.duplicates} "
        f"provenance_added={summary.provenance_added} provenance_existing={summary.provenance_existing}"
    )


@app.command("discover-chamber")
def discover_chamber(
    limit: int = typer.Option(40, min=1, max=60),
    directory_url: str = typer.Option("https://business.daltonchamber.org/list/"),
) -> None:
    """Discover a bounded local-SMB sample from a public GrowthZone directory."""
    settings = load_settings()
    source = GrowthZoneDirectorySource(
        directory_url=directory_url, limit=limit,
        delay_seconds=settings.website.delay_between_requests_seconds,
        timeout_seconds=settings.website.request_timeout_seconds,
        user_agent=settings.website.user_agent,
    )
    with db_session() as session:
        summary = ingest(session, source)
    typer.echo(
        f"processed={summary.processed} created={summary.created} duplicates={summary.duplicates} "
        f"provenance_added={summary.provenance_added} provenance_existing={summary.provenance_existing}"
    )


@app.command("inspect-websites")
def inspect_websites(
    limit: int | None = typer.Option(None, min=1, help="Override the configured company cap."),
    refresh: bool = typer.Option(False, help="Ignore fresh cached snapshots and fetch again."),
) -> None:
    """Collect bounded public website evidence sequentially."""
    settings = load_settings()
    safe_limit = min(limit or settings.website.max_companies_per_run, 100)
    with db_session() as session:
        summary = collect_websites(session, settings.website, limit=safe_limit, refresh=refresh)
    typer.echo(
        f"processed={summary.processed} fetched={summary.fetched} cached={summary.cached} "
        f"blocked={summary.blocked} failed={summary.failed}"
    )


@app.command("enrich-contacts")
def enrich_contact_records(
    priority: str = typer.Option("HIGH,MEDIUM", help="Comma-separated machine priority tiers."),
    review: str | None = typer.Option(None, help="Optional comma-separated company review statuses."),
    limit: int = typer.Option(25, min=1, max=100),
) -> None:
    """Discover contacts from stored, explicit first-party public evidence only."""
    try:
        reviews = [ReviewStatus(item) for item in _csv_values(review)] if review else None
    except ValueError as exc:
        raise typer.BadParameter("review must contain PENDING, APPROVED, REJECTED, or MAYBE") from exc
    with db_session() as session:
        summary = enrich_contacts(session, priorities=_csv_values(priority), reviews=reviews, limit=limit)
    typer.echo(
        f"processed={summary.processed} found={summary.found} not_found={summary.not_found} "
        f"blocked={summary.blocked} contacts_created={summary.contacts_created} duplicates={summary.duplicates}"
    )


@app.command("website-evidence")
def website_evidence(company_id: int) -> None:
    """Show the latest stored evidence for one company."""
    with db_session() as session:
        snapshot = session.scalar(
            select(WebsiteSnapshot)
            .where(WebsiteSnapshot.company_id == company_id)
            .order_by(WebsiteSnapshot.fetched_at.desc())
        )
        if snapshot is None:
            raise typer.BadParameter(f"Company {company_id} has no website evidence")
        typer.echo(
            f"snapshot_id: {snapshot.id}\nstatus: {snapshot.fetch_status}\nfetched_at: {snapshot.fetched_at.isoformat()}\n"
            f"requested_url: {snapshot.requested_url}\nfinal_url: {snapshot.final_url or '-'}\n"
            f"http_status: {snapshot.http_status}\ntitle: {snapshot.title or '-'}\n"
            f"visible_text_length: {snapshot.visible_text_length}\nrobots_present: {snapshot.robots_present}\n"
            f"sitemap_present: {snapshot.sitemap_present}\nstructured_data_types: {snapshot.structured_data_types}\n"
            f"signals: {snapshot.extracted_signals}\nerror: {snapshot.error_type or '-'} {snapshot.error_message or ''}\n"
            f"pages_inspected: {len(snapshot.pages)}"
        )


@app.command()
def qualify(
    config: Path = typer.Option(Path("config/qualification.yaml"), exists=True, dir_okay=False),
    force: bool = typer.Option(False, help="Re-evaluate companies already qualified."),
    requalify: bool = typer.Option(False, help="Recompute machine results; human review is preserved."),
) -> None:
    """Apply transparent rule-based qualification."""
    with db_session() as session:
        counts = qualify_companies(session, config, force=force or requalify)
    typer.echo(" ".join(f"{key}={value}" for key, value in counts.items()))


@companies_app.command("list")
def list_companies(status: str | None = typer.Option(None, help="Filter by status.")) -> None:
    with db_session() as session:
        query = select(Company).order_by(Company.id)
        if status:
            query = query.where(Company.pipeline_status == status.upper())
        rows = session.scalars(query).all()
        typer.echo("ID  STATUS         SCORE  DOMAIN                       NAME")
        for company in rows:
            score = "-" if company.qualification_score is None else f"{company.qualification_score:.0f}"
            typer.echo(f"{company.id:<3} {company.pipeline_status.value:<14} {score:<6} {company.normalized_domain:<28} {company.company_name}")


@companies_app.command("show")
def show_company(company_id: int) -> None:
    with db_session() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise typer.BadParameter(f"Company {company_id} does not exist")
        typer.echo(f"id: {company.id}\nname: {company.company_name}\nwebsite: {company.website}")
        typer.echo(f"domain: {company.normalized_domain}\nstatus: {company.pipeline_status.value}")
        typer.echo(f"score: {company.qualification_score}\nreason: {company.qualification_reason or '-'}")
        typer.echo(
            f"eligibility: {company.eligibility or '-'}\ngeo_opportunity: {company.geo_opportunity or '-'}\n"
            f"priority: {company.priority_tier or '-'} ({company.priority_score})\n"
            f"confidence: {company.evidence_confidence or '-'}\nreview: {company.review_status.value}"
        )
        typer.echo("provenance:")
        for discovery in company.discoveries:
            typer.echo(f"  - {discovery.source_type}: {discovery.source_identifier} at {discovery.discovered_at.isoformat()}")


@review_app.command("list")
def review_list(status: ReviewStatus | None = typer.Option(None)) -> None:
    with db_session() as session:
        query = select(Company).order_by(Company.priority_score.desc().nullslast(), Company.id)
        if status:
            query = query.where(Company.review_status == status)
        rows = session.scalars(query).all()
        typer.echo("ID  REVIEW    PRIORITY SCORE COMPANY")
        for row in rows:
            typer.echo(f"{row.id:<3} {row.review_status.value:<9} {(row.priority_tier or '-'):<8} {(row.priority_score or 0):>5.0f} {row.company_name}")


@review_app.command("show")
def review_show(company_id: int) -> None:
    show_company(company_id)


@review_app.command("set")
def review_set(
    company_id: int,
    status: ReviewStatus,
    notes: str | None = typer.Option(None),
    reviewed_by: str | None = typer.Option(None),
) -> None:
    with db_session() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise typer.BadParameter(f"Company {company_id} does not exist")
        company.review_status = status
        company.review_notes = notes
        company.reviewed_by = reviewed_by
        company.reviewed_at = datetime.now(timezone.utc)
        session.commit()
    typer.echo(f"company_id={company_id} review_status={status.value}")


@contacts_app.command("list")
def contacts_list(company_id: int | None = typer.Option(None), review: ReviewStatus | None = typer.Option(None)) -> None:
    with db_session() as session:
        query = select(Contact).order_by(Contact.company_id, Contact.ranking_score.desc(), Contact.id)
        if company_id is not None:
            query = query.where(Contact.company_id == company_id)
        if review is not None:
            query = query.where(Contact.review_status == review)
        rows = session.scalars(query).all()
        typer.echo("ID  COMPANY SCORE TYPE                      ROLE                 EMAIL / NAME")
        for row in rows:
            typer.echo(f"{row.id:<3} {row.company_id:<7} {row.ranking_score:>5.0f} {row.contact_type.value:<25} {row.normalized_role:<20} {row.email or row.name or '-'}")


@contacts_app.command("show")
def contacts_show(contact_id: int) -> None:
    with db_session() as session:
        row = session.get(Contact, contact_id)
        if row is None:
            raise typer.BadParameter(f"Contact {contact_id} does not exist")
        typer.echo(
            f"id: {row.id}\ncompany_id: {row.company_id}\nname: {row.name or '-'}\ntitle: {row.title or '-'}\n"
            f"role: {row.normalized_role}\ntype: {row.contact_type.value}\nemail: {row.email or '-'}\n"
            f"validation: {row.validation_status}\nconfidence: {row.confidence}\nrank: {row.ranking_score}\n"
            f"reason: {row.ranking_reason or '-'}\nsource: {row.source_url or '-'}\nreview: {row.review_status.value}"
        )


@contacts_app.command("review-set")
def contact_review_set(
    contact_id: int, status: ReviewStatus, notes: str | None = typer.Option(None),
    reviewed_by: str | None = typer.Option(None),
) -> None:
    with db_session() as session:
        row = session.get(Contact, contact_id)
        if row is None:
            raise typer.BadParameter(f"Contact {contact_id} does not exist")
        row.review_status = status
        row.review_notes = notes
        row.reviewed_by = reviewed_by
        row.reviewed_at = datetime.now(timezone.utc)
        session.commit()
    typer.echo(f"contact_id={contact_id} review_status={status.value}")


@candidates_app.command("rank")
def candidates_rank(
    limit: int = typer.Option(20, min=1, max=100),
    priority: str | None = typer.Option(None),
    review: ReviewStatus | None = typer.Option(None),
    source: str | None = typer.Option(None),
) -> None:
    with db_session() as session:
        rows = session.scalars(select(Company).order_by(Company.priority_score.desc().nullslast(), Company.id)).unique().all()
        if priority:
            rows = [row for row in rows if row.priority_tier == priority.upper()]
        if review:
            rows = [row for row in rows if row.review_status == review]
        if source:
            rows = [row for row in rows if any(source.casefold() in d.source_type.casefold() or source.casefold() in (d.source_name or "").casefold() for d in row.discoveries)]
        typer.echo("RANK ID  PRIORITY SCORE CONFIDENCE REVIEW    INDUSTRY              COMPANY")
        for rank, row in enumerate(rows[:limit], 1):
            typer.echo(f"{rank:<4} {row.id:<3} {(row.priority_tier or '-'):<8} {(row.priority_score or 0):>5.0f} {(row.evidence_confidence or '-'):<10} {row.review_status.value:<9} {(row.industry_normalized or 'Unknown'):<21} {row.company_name}")


@app.command("export-review")
def export_review(output: Path = typer.Option(Path("data/research_partner_review.csv"))) -> None:
    """Export a human-readable review surface; SQLite remains canonical."""
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "company_id", "company_name", "website", "normalized_domain", "industry", "industry_raw", "location",
        "company_size_category", "company_size_value", "company_size_source", "discovery_sources",
        "website_fetch_status", "eligibility", "qualification_score", "geo_opportunity", "priority_score",
        "priority_tier", "confidence", "qualification_reason", "geo_opportunity_reason", "key_evidence",
        "pipeline_status", "review_status", "review_notes", "reviewed_by", "last_inspected_at",
    ]
    with db_session() as session, output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for company in session.scalars(select(Company).order_by(Company.priority_score.desc().nullslast(), Company.id)).all():
            snapshot = session.scalar(select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id).order_by(WebsiteSnapshot.fetched_at.desc()))
            evidence = json.loads(company.qualification_evidence or "{}")
            writer.writerow({
                "company_id": company.id, "company_name": company.company_name, "website": company.website,
                "normalized_domain": company.normalized_domain, "industry": company.industry_normalized,
                "industry_raw": company.industry_raw, "location": company.location,
                "company_size_category": company.company_size_category, "company_size_value": company.company_size_value,
                "company_size_source": company.company_size_source,
                "discovery_sources": "; ".join(sorted({d.source_name or d.source_type for d in company.discoveries})),
                "website_fetch_status": snapshot.fetch_status if snapshot else "NOT_INSPECTED",
                "eligibility": company.eligibility, "qualification_score": company.qualification_score,
                "geo_opportunity": company.geo_opportunity, "priority_score": company.priority_score,
                "priority_tier": company.priority_tier, "confidence": company.evidence_confidence,
                "qualification_reason": company.qualification_reason, "geo_opportunity_reason": company.geo_opportunity_reason,
                "key_evidence": "; ".join((evidence.get("opportunity") or []) + (evidence.get("uncertainty") or [])),
                "pipeline_status": company.pipeline_status.value, "review_status": company.review_status.value,
                "review_notes": company.review_notes, "reviewed_by": company.reviewed_by,
                "last_inspected_at": snapshot.fetched_at.isoformat() if snapshot else "",
            })
    typer.echo(f"Exported review dataset to {output}")


@app.command("import-review")
def import_review(source: Path = typer.Option(..., exists=True, dir_okay=False)) -> None:
    """Import only allowlisted human-review fields from an edited review CSV."""
    updated = 0
    with source.open(newline="", encoding="utf-8-sig") as handle, db_session() as session:
        for row in csv.DictReader(handle):
            try:
                company_id = int(row.get("company_id", ""))
                status = ReviewStatus(row.get("review_status", "").strip().upper())
            except (ValueError, TypeError) as exc:
                raise typer.BadParameter(f"Invalid company_id or review_status in row: {row}") from exc
            company = session.get(Company, company_id)
            if company is None:
                raise typer.BadParameter(f"Company {company_id} does not exist")
            company.review_status = status
            company.review_notes = row.get("review_notes") or None
            company.reviewed_by = row.get("reviewed_by") or None
            company.reviewed_at = datetime.now(timezone.utc)
            updated += 1
        session.commit()
    typer.echo(f"Imported review fields for {updated} companies; canonical fields were ignored")


@app.command("calibration-report")
def calibration_report() -> None:
    """Show absolute priority distributions by source and normalized industry."""
    with db_session() as session:
        companies = session.scalars(select(Company).order_by(Company.id)).unique().all()
        typer.echo("OVERALL PRIORITY")
        for tier in ("HIGH", "MEDIUM", "LOW", "NEEDS_REVIEW"):
            typer.echo(f"{tier}={sum(c.priority_tier == tier for c in companies)}")
        typer.echo("\nBY SOURCE")
        sources = sorted({d.source_type for c in companies for d in c.discoveries})
        for source in sources:
            group = [c for c in companies if any(d.source_type == source for d in c.discoveries)]
            distribution = " ".join(f"{tier}={sum(c.priority_tier == tier for c in group)}" for tier in ("HIGH", "MEDIUM", "LOW", "NEEDS_REVIEW"))
            typer.echo(f"{source}: n={len(group)} {distribution}")
        typer.echo("\nBY INDUSTRY")
        for industry in sorted({c.industry_normalized or "Unknown" for c in companies}):
            group = [c for c in companies if (c.industry_normalized or "Unknown") == industry]
            typer.echo(f"{industry}: n={len(group)} avg_priority={sum(c.priority_score or 0 for c in group)/len(group):.1f}")


@app.command()
def status() -> None:
    """Summarize database and safety state."""
    settings = load_settings()
    with db_session() as session:
        total = session.scalar(select(func.count(Company.id))) or 0
        provenance = session.scalar(select(func.count(DiscoveryRecord.id))) or 0
        rows = session.execute(select(Company.pipeline_status, func.count()).group_by(Company.pipeline_status)).all()
    typer.echo(f"database={settings.database_url}\nsend_mode={settings.send_mode}\ncompanies={total}\nprovenance_records={provenance}")
    for company_status, count in rows:
        typer.echo(f"{company_status.value}={count}")
