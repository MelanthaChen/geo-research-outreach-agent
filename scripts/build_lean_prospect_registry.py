#!/usr/bin/env python3
"""Build an isolated lean Outreach prospect registry from immutable sources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.lean_registry import promote_sources


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-db", type=Path, default=Path("data/phase7_candidates.db"))
    parser.add_argument("--hotel-db", type=Path, default=Path("data/hotel_candidates.db"))
    parser.add_argument("--target", required=True, help="isolated SQLite path or PostgreSQL URL")
    parser.add_argument("--existing-registry", help="optional registry used only to preserve prior-contact identifiers")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = promote_sources(business_db=args.business_db, hotel_db=args.hotel_db, target=args.target, existing_registry=args.existing_registry)
    output = json.dumps(report, indent=2, sort_keys=True)
    print(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
