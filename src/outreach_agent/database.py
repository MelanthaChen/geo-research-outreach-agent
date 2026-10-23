"""Small database compatibility layer for the durable Research Registry.

The discovery/source stores remain SQLite read-only inputs. The lifecycle store
can use SQLite locally or PostgreSQL in a cloud deployment without changing
the lifecycle service's parameterized SQL API.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from outreach_agent.models import Base

POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def create_db_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url)


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    # Keep the existing lightweight schema migrations used by the source-store
    # workflows. These are separate from the lifecycle registry connection.
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
            "channel_override_contact_id": "INTEGER", "channel_override_reason": "TEXT",
            "contact_status": "VARCHAR(30) DEFAULT 'NOT_RUN'", "contacts_enriched_at": "DATETIME",
            "contactability_status": "VARCHAR(40) DEFAULT 'NO_SUITABLE_CHANNEL'",
            "primary_channel_id": "INTEGER",
        }
        with engine.begin() as connection:
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE companies ADD COLUMN {name} {definition}"))
    if "website_pages" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("website_pages")}
        with engine.begin() as connection:
            for name in ("contact_evidence", "extracted_channels"):
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE website_pages ADD COLUMN {name} TEXT DEFAULT '[]'"))
    if "contacts" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("contacts")}
        additions = {
            "normalized_name": "VARCHAR(255)", "normalized_role": "VARCHAR(50) DEFAULT 'UNKNOWN'",
            "contact_type": "VARCHAR(40) DEFAULT 'GENERIC_BUSINESS_CONTACT'", "confidence": "VARCHAR(50) DEFAULT 'LOW'",
            "ranking_score": "FLOAT DEFAULT 0", "ranking_reason": "TEXT", "review_status": "VARCHAR(20) DEFAULT 'PENDING'",
            "review_notes": "TEXT", "reviewed_at": "DATETIME", "reviewed_by": "VARCHAR(255)",
            "discovered_at": "DATETIME", "extraction_method": "VARCHAR(50) DEFAULT 'LIGHTWEIGHT_HTML'",
            "evidence_text": "TEXT", "channel_type": "VARCHAR(40)", "channel_url": "VARCHAR(2048)",
            "purpose": "TEXT", "recommended": "BOOLEAN DEFAULT 0",
        }
        with engine.begin() as connection:
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE contacts ADD COLUMN {name} {definition}"))


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


class HybridRow(dict):
    """Mapping row that also preserves SQLite-style integer indexing."""

    def __getitem__(self, key: Any):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def _row(self, row):
        if row is None:
            return None
        if isinstance(row, dict):
            return HybridRow(row)
        return row

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def _qmark_to_postgres(sql: str) -> str:
    return sql.replace("?", "%s")


def _params_to_postgres(sql: str, params):
    """Translate the registry's SQLite qmarks and named mappings for psycopg."""
    if isinstance(params, dict):
        return re.sub(r":([A-Za-z_]\w*)", r"%(\1)s", sql)
    return _qmark_to_postgres(sql)


class PostgresConnection:
    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised in deployment
            raise RuntimeError("PostgreSQL support requires: pip install 'psycopg[binary]>=3.2,<4'") from exc
        # Render/Blueprint secret fields can preserve a final newline when a
        # connection URL is pasted from a downloaded .env file.  Strip only
        # surrounding whitespace so valid query parameters and credentials are
        # otherwise left untouched before handing the URL to psycopg.
        self._connection = psycopg.connect(url.strip(), row_factory=dict_row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()

    def execute(self, sql: str, params=()):
        pragma = re.fullmatch(r"\s*PRAGMA\s+table_info\((\w+)\)\s*", sql, re.I)
        if pragma:
            table = pragma.group(1)
            cursor = self._connection.execute(
                "SELECT ordinal_position - 1 AS cid, column_name AS name, data_type AS type, "
                "CASE WHEN is_nullable = 'NO' THEN 1 ELSE 0 END AS notnull, "
                "column_default AS dflt_value, ordinal_position AS pk "
                "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,),
            )
            return PostgresCursor(cursor)
        cursor = self._connection.execute(_params_to_postgres(sql, params), params)
        return PostgresCursor(cursor)

    def executemany(self, sql: str, params):
        cursor = self._connection.cursor()
        cursor.executemany(_qmark_to_postgres(sql), params)
        return PostgresCursor(cursor)

    def executescript(self, script: str):
        for statement in (item.strip() for item in script.split(";")):
            if statement:
                self.execute(statement)
        return self

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def is_postgres_target(path: str | Path) -> bool:
    return isinstance(path, str) and path.startswith(POSTGRES_PREFIXES)


def connect_database(path: str | Path):
    if is_postgres_target(path):
        return PostgresConnection(path)
    return sqlite3.connect(path)
