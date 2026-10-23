#!/usr/bin/env python3
"""Run one bounded, resumable hotel prospect discovery/promotion pass."""

from __future__ import annotations

import argparse
import json
import os

from outreach_agent.hotel_prospect_job import pause, resume, run_once, status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "pause", "resume"))
    parser.add_argument("--registry", default=os.environ.get("OUTREACH_DATABASE_URL", "data/research_registry.db"))
    parser.add_argument("--source", default=os.environ.get("HOTEL_DISCOVERY_SOURCE", ""))
    parser.add_argument("--source-mode", type=str.upper, choices=("AUTO", "MANIFEST", "OVERPASS"),
                        default=os.environ.get("HOTEL_DISCOVERY_SOURCE_MODE", "AUTO").upper())
    parser.add_argument("--job-id", default="hotel-prospect-discovery")
    parser.add_argument("--target", type=int, default=int(os.environ.get("HOTEL_PROSPECT_TARGET", "1000")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("HOTEL_DISCOVERY_BATCH_SIZE", "25")))
    parser.add_argument("--reason", default="operator_pause")
    args = parser.parse_args()
    if args.command == "run":
        if os.environ.get("HOTEL_DISCOVERY_ENABLED", "false").casefold() != "true":
            raise SystemExit("hotel discovery is disabled; set HOTEL_DISCOVERY_ENABLED=true for an explicit staging/operator run")
        if args.source_mode == "MANIFEST" and not args.source:
            raise SystemExit("--source or HOTEL_DISCOVERY_SOURCE is required for manifest mode")
        result = run_once(args.registry, args.source or None, job_id=args.job_id,
                          target_count=args.target, batch_size=args.batch_size,
                          source_mode=args.source_mode)
    elif args.command == "status":
        result = status(args.registry, args.job_id)
    elif args.command == "pause":
        result = pause(args.registry, args.reason, args.job_id)
    else:
        result = resume(args.registry, args.job_id)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
