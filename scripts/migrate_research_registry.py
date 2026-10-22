#!/usr/bin/env python3
"""Copy the Outreach Research Registry from SQLite to PostgreSQL.

The source is opened read-only and is never altered. The target must be a
staging/production PostgreSQL URL supplied by the operator; this command does
not create databases, credentials, or cloud resources.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from outreach_agent.database import _qmark_to_postgres, connect_database
from outreach_agent.research_lifecycle import init_registry


def table_names(source: sqlite3.Connection) -> list[str]:
    return [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def migrate(source_path: Path, target_url: str, *, dry_run: bool = False) -> dict[str, int]:
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    try:
        tables = table_names(source)
        counts = {table: source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        if dry_run:
            return counts
        init_registry(target_url)
        with connect_database(target_url) as target:
            for table in tables:
                columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()]
                quoted_columns = ",".join(f'"{column}"' for column in columns)
                marks = ",".join("?" for _ in columns)
                rows = source.execute(f'SELECT {quoted_columns} FROM "{table}"').fetchall()
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
