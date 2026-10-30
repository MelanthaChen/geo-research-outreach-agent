#!/usr/bin/env python3
"""Explicit V2 initialization, one-shot worker and web entry point."""
import argparse
import json
import os
import sys

from outreach_v2.db import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "serve", "tick", "personalize"])
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8020")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--candidate-id", action="append", default=[],
                        help="Exact candidate ID in the authorized five-company cohort; repeat once per company")
    parser.add_argument("--reprocess-candidate-id", action="append", default=[],
                        help="Explicitly reprocess an eligible, uninvited historical READY candidate within the cohort")
    args = parser.parse_args()
    db = Database()
    if args.command == "init":
        db.initialize()
    elif args.command == "tick":
        from outreach_v2.worker import tick

        result = tick(db)
        print(result)
        if result["status"] != "COMPLETED":
            raise SystemExit(1)
    elif args.command == "personalize":
        # This path runs only the existing, persisted semantic personalization
        # service; it does not ingest consent, hand off GEO jobs, or deliver reports.
        db.initialize()
        from outreach_v2.personalization import backfill
        candidate_ids=args.candidate_id or [x.strip() for x in os.environ.get(
            'OUTREACH_AI_PERSONALIZATION_COHORT_CANDIDATE_IDS','').split(',') if x.strip()]
        reprocess_ids=args.reprocess_candidate_id or [x.strip() for x in os.environ.get(
            'OUTREACH_AI_PERSONALIZATION_REPROCESS_CANDIDATE_IDS','').split(',') if x.strip()]
        limit=min(5,int(os.environ.get('OUTREACH_AI_PERSONALIZATION_MAX_CANDIDATES_PER_RUN','5')))
        result=backfill(db,limit=limit,concurrency=1,time_budget_seconds=300,
            authorized_candidate_ids=candidate_ids,selected_candidate_ids=reprocess_ids)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        if result.get('status') not in {'COMPLETED','DISABLED'}:
            raise SystemExit(1)
    else:
        import uvicorn

        from outreach_v2.app import create_app

        # Apply additive, transaction-safe schema migrations before serving
        # authenticated worker requests.
        db.initialize()
        uvicorn.run(create_app(db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
