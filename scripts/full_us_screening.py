#!/usr/bin/env python3
"""Plan, execute, export, and summarize the complete US screening run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from outreach_agent.full_screening import export_full_run, plan_full_run, run_full_job, summarize


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "run", "export", "all"])
    parser.add_argument("--job-id")
    parser.add_argument("--registry", type=Path, default=Path("data/research_registry.db"))
    args = parser.parse_args()
    job_id = args.job_id or plan_full_run(args.registry)
    result = {"job_id": job_id}
    if args.command in {"run", "all"}:
        result["run"] = run_full_job(job_id, args.registry)
    if args.command in {"export", "all"}:
        result["export"] = export_full_run(job_id, args.registry)
    result["summary"] = summarize(job_id, args.registry)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
