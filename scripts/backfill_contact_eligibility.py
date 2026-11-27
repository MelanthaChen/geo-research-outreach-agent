"""Initialize/repair the read-only contact eligibility projection.

This is an explicit maintenance command. It never runs on an HTTP request and
requires --apply before opening a database or creating/updating schema.
"""
import argparse
import sys
import time

from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--apply',action='store_true',help='Apply schema setup and recompute the projection')
    args=parser.parse_args()
    if not args.apply:
        parser.error('Refusing to connect or write without --apply')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from outreach_v2.db import Database
    from outreach_v2.service import backfill_contact_eligibility_cache
    db=Database(args.url)
    try:
        db.initialize()
        started=time.perf_counter()
        count=backfill_contact_eligibility_cache(db)
        elapsed_ms=round((time.perf_counter()-started)*1000,3)
        print({'initialized':True,'authorized_candidate_count':count,'elapsed_ms':elapsed_ms})
    finally:
        db.engine.dispose()


if __name__=='__main__':main()
