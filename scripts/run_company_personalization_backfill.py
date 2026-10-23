#!/usr/bin/env python3
"""Run the resumable rule-based company/hotel personalization backfill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.personalization_backfill import create_job, run_job


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--job-id")
    parser.add_argument("--request-budget", type=int, default=100)
    parser.add_argument("--pause-after", type=int)
    args = parser.parse_args()
    job_id = create_job(args.db, limit=args.limit, request_budget=args.request_budget, job_id=args.job_id)
    print(json.dumps(run_job(job_id, args.db, pause_after=args.pause_after), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
