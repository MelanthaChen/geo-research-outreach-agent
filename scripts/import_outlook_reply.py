#!/usr/bin/env python3
"""Import an Outlook-exported .eml reply as append-only review evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.research_lifecycle import REGISTRY_DB, ingest_eml_reply


def main() -> int:
    parser = argparse.ArgumentParser(description="Import one Outlook .eml reply without granting consent")
    parser.add_argument("eml", type=Path)
    parser.add_argument("--db", type=Path, default=REGISTRY_DB)
    args = parser.parse_args()
    result = ingest_eml_reply(args.eml, path=args.db)
    print(json.dumps({key: value for key, value in result.items() if key != "raw_content"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
