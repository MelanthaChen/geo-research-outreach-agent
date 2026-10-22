"""Run a capped pilot for official website discovery on hotels missing URLs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.hotel_website_discovery import discover_hotel_websites
from outreach_agent.hotels import HOTEL_DB, HOTEL_PBF


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot", choices=("pilot",))
    parser.add_argument("--limit", type=int, default=25, help="Maximum exact OSM→Wikidata property links (1..25)")
    parser.add_argument("--pbf", type=Path, default=HOTEL_PBF)
    parser.add_argument("--database", type=Path, default=HOTEL_DB)
    parser.add_argument("--apply", action="store_true", help="Add name-matched P856 URLs as unverified candidates")
    args = parser.parse_args()
    result = discover_hotel_websites(pbf=args.pbf, database=args.database, limit=args.limit, apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
