#!/usr/bin/env python3
"""Plan and run bounded, resumable pre-consent website screening."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.preliminary_screening import MockScreeningProvider, ScreeningConfig, audit_readiness, plan_job, run_job


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("audit")
    audit.add_argument("--registry", type=Path, default=Path("data/research_registry.db"))
    plan = sub.add_parser("plan")
    plan.add_argument("--limit", type=int, default=10)
    plan.add_argument("--registry", type=Path, default=Path("data/research_registry.db"))
    plan.add_argument("--provider", choices=["mock"], default="mock")
    plan.add_argument("--live-fetch", action="store_true", help="Use bounded public fetches; never enables email or GEO Engine actions")
    run = sub.add_parser("run")
    run.add_argument("job_id")
    run.add_argument("--registry", type=Path, default=Path("data/research_registry.db"))
    run.add_argument("--live-fetch", action="store_true")
    run.add_argument("--pause-after", type=int)
    args = parser.parse_args()
    if args.command == "audit":
        result = audit_readiness(args.registry)
    elif args.command == "plan":
        result = {"job_id": plan_job(args.registry, limit=args.limit, provider=args.provider, dry_run=not args.live_fetch)}
    else:
        result = run_job(args.job_id, args.registry, provider=MockScreeningProvider(), config=ScreeningConfig(), mock_fetch=not args.live_fetch, pause_after=args.pause_after)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
