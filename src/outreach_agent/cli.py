from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from outreach_agent.config import load_settings
from outreach_agent.database import create_db_engine, init_database, session_factory
from outreach_agent.discovery import YCPublicDirectorySource, source_for
from outreach_agent.logging_config import configure_logging
from outreach_agent.models import Company, DiscoveryRecord, WebsiteSnapshot
from outreach_agent.qualification import qualify_companies
from outreach_agent.services import ingest
from outreach_agent.website import collect_websites

app = typer.Typer(help="GEO research outreach foundation (no email sending).")
companies_app = typer.Typer(help="Inspect discovered companies.")
app.add_typer(companies_app, name="companies")


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


@app.command("inspect-websites")
def inspect_websites(
    limit: int | None = typer.Option(None, min=1, help="Override the configured company cap."),
    refresh: bool = typer.Option(False, help="Ignore fresh cached snapshots and fetch again."),
) -> None:
    """Collect bounded public website evidence sequentially."""
    settings = load_settings()
    safe_limit = min(limit or settings.website.max_companies_per_run, 50)
    with db_session() as session:
        summary = collect_websites(session, settings.website, limit=safe_limit, refresh=refresh)
    typer.echo(
        f"processed={summary.processed} fetched={summary.fetched} cached={summary.cached} "
        f"blocked={summary.blocked} failed={summary.failed}"
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
) -> None:
    """Apply transparent rule-based qualification."""
    with db_session() as session:
        counts = qualify_companies(session, config, force=force)
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
        typer.echo("provenance:")
        for discovery in company.discoveries:
            typer.echo(f"  - {discovery.source_type}: {discovery.source_identifier} at {discovery.discovered_at.isoformat()}")


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
