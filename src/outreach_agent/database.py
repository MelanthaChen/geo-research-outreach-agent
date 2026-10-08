from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from outreach_agent.models import Base


def create_db_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url)


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    # Lightweight Phase 1 -> Phase 2 migration. A formal migration tool can follow
    # once schema changes become frequent.
    if "discovery_records" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("discovery_records")}
        with engine.begin() as connection:
            if "source_name" not in columns:
                connection.execute(text("ALTER TABLE discovery_records ADD COLUMN source_name VARCHAR(255)"))
            if "source_url" not in columns:
                connection.execute(text("ALTER TABLE discovery_records ADD COLUMN source_url VARCHAR(2048)"))
    if "companies" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("companies")}
        additions = {
            "industry_raw": "VARCHAR(255)", "industry_normalized": "VARCHAR(100)",
            "company_size_category": "VARCHAR(50) DEFAULT 'UNKNOWN'", "company_size_value": "INTEGER",
            "company_size_source": "VARCHAR(50) DEFAULT 'unknown'", "eligibility": "VARCHAR(50)",
            "geo_opportunity_reason": "TEXT", "priority_score": "FLOAT", "priority_tier": "VARCHAR(50)",
            "evidence_confidence": "VARCHAR(50)", "qualification_evidence": "TEXT",
            "review_status": "VARCHAR(20) DEFAULT 'PENDING'", "review_notes": "TEXT",
            "reviewed_at": "DATETIME", "reviewed_by": "VARCHAR(255)",
            "contact_status": "VARCHAR(30) DEFAULT 'NOT_RUN'", "contacts_enriched_at": "DATETIME",
        }
        with engine.begin() as connection:
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {sql_type}"))
    if "website_pages" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("website_pages")}
        if "contact_evidence" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE website_pages ADD COLUMN contact_evidence TEXT DEFAULT '[]'"))
    if "contacts" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("contacts")}
        additions = {
            "normalized_name": "VARCHAR(255)", "normalized_role": "VARCHAR(50) DEFAULT 'UNKNOWN'",
            "contact_type": "VARCHAR(40) DEFAULT 'GENERIC_BUSINESS_CONTACT'",
            "confidence": "VARCHAR(50) DEFAULT 'LOW'", "ranking_score": "FLOAT DEFAULT 0",
            "ranking_reason": "TEXT", "review_status": "VARCHAR(20) DEFAULT 'PENDING'",
            "review_notes": "TEXT", "reviewed_at": "DATETIME", "reviewed_by": "VARCHAR(255)",
            "discovered_at": "DATETIME",
        }
        with engine.begin() as connection:
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE contacts ADD COLUMN {name} {sql_type}"))


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
