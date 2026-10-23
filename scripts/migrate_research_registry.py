#!/usr/bin/env python3
"""Copy the Outreach Research Registry from SQLite to PostgreSQL.

The source is opened read-only and is never altered. The target must be a
staging/production PostgreSQL URL supplied by the operator; this command does
not create databases, credentials, or cloud resources.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path

from outreach_agent.database import _qmark_to_postgres, connect_database
from outreach_agent.research_lifecycle import init_registry


def table_names(source: sqlite3.Connection) -> list[str]:
    return [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _postgres_type(sqlite_type: str | None) -> str:
    """Map the small SQLite type vocabulary used by the registry to PostgreSQL."""
    normalized = (sqlite_type or "TEXT").upper()
    if "INT" in normalized:
        return "INTEGER"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BOOL" in normalized:
        return "BOOLEAN"
    if "BLOB" in normalized:
        return "BYTEA"
    return "TEXT"


def ensure_missing_tables(source: sqlite3.Connection, target, tables: list[str]) -> None:
    """Create source tables not covered by the lifecycle initializer.

    The registry initializer owns the current lifecycle schema. Source files
    can also contain historical screening tables (for example
    ``full_screening_jobs``) that are intentionally not imported by the
    lifecycle service at runtime. They still need durable read-model tables so
    migration preserves the source registry without requiring a second schema
    definition. This is metadata-only DDL; existing target tables are never
    altered.
    """
    existing = {
        row[0]
        for row in target.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ).fetchall()
    }
    for table in tables:
        if table in existing:
            continue
        columns = source.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not columns:
            continue
        definitions: list[str] = []
        primary_keys = []
        for _, name, type_name, notnull, default, pk in columns:
            definition = f'"{name}" {_postgres_type(type_name)}'
            if notnull:
                definition += " NOT NULL"
            if default is not None and str(default).upper() in {"0", "1"} and "INT" in (type_name or "").upper():
                definition += f" DEFAULT {default}"
            if pk:
                primary_keys.append(name)
            definitions.append(definition)
        if primary_keys:
            quoted = ",".join(f'"{name}"' for name in primary_keys)
            definitions.append(f"PRIMARY KEY ({quoted})")
        target.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({",".join(definitions)})')
        existing.add(table)


def normalize_legacy_row(table: str, columns: list[str], row: tuple) -> tuple:
    """Preserve legacy rows while satisfying current lifecycle constraints."""
    if table != "questionnaire_invitations":
        return row
    values = list(row)
    if "invitation_reference" not in columns or len(values) <= columns.index("invitation_reference"):
        invitation_id = str(values[columns.index("invitation_id")])
        digest = hashlib.sha256(invitation_id.encode("utf-8")).hexdigest()[:16].upper()
        return tuple(values + [f"LEGACY-{digest}"])
    reference_index = columns.index("invitation_reference")
    if values[reference_index] is None:
        invitation_index = columns.index("invitation_id")
        invitation_id = str(values[invitation_index])
        digest = hashlib.sha256(invitation_id.encode("utf-8")).hexdigest()[:16].upper()
        values[reference_index] = f"LEGACY-{digest}"
    return tuple(values)


def migrate(source_path: Path, target_url: str, *, dry_run: bool = False) -> dict[str, int]:
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    try:
        tables = table_names(source)
        counts = {table: source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        if dry_run:
            return counts
        init_registry(target_url)
        with connect_database(target_url) as target:
            if target_url.startswith(("postgres://", "postgresql://")):
                ensure_missing_tables(source, target, tables)
            for table in tables:
                source_columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()]
                columns = list(source_columns)
                if table == "questionnaire_invitations" and "invitation_reference" not in columns:
                    columns.append("invitation_reference")
                quoted_columns = ",".join(f'"{column}"' for column in columns)
                marks = ",".join("?" for _ in columns)
                source_quoted_columns = ",".join(f'"{column}"' for column in source_columns)
                rows = source.execute(f'SELECT {source_quoted_columns} FROM "{table}"').fetchall()
                rows = [normalize_legacy_row(table, columns, row) for row in rows]
                if rows:
                    target.executemany(
                        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({marks}) ON CONFLICT DO NOTHING',
                        rows,
                    )
            target.commit()
        return counts
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/research_registry.db"))
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"source registry does not exist: {args.source}")
    counts = migrate(args.source, args.target_url, dry_run=args.dry_run)
    print("migration_dry_run=" + str(args.dry_run).lower())
    print("tables=" + str(len(counts)) + " rows=" + str(sum(counts.values())))
    for table, count in counts.items():
        print(f"{table}={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
