"""Incrementally ingest Phase 7 business and hotel facts into the research registry."""

import argparse
from pathlib import Path

from outreach_agent.research_lifecycle import ingest_candidate_stores


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--business-db", type=Path, default=Path("data/phase7_candidates.db"))
    parser.add_argument("--hotel-db", type=Path, default=Path("data/hotel_candidates.db"))
    parser.add_argument("--registry-db", type=Path, default=Path("data/research_registry.db"))
    args = parser.parse_args()
    print(ingest_candidate_stores(business_db=args.business_db, hotel_db=args.hotel_db, registry_db=args.registry_db))


if __name__ == "__main__":
    main()

