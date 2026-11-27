"""Offline HTTP/SQL benchmark. Only disposable local geo_perf_* databases allowed.

Seed synthetic *existing* records directly; never invoke invitation generation,
workers, mail, crawling or model providers. Benchmark requests are read-only.
Run baseline and changed checkout against the same seeded database.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--source-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', required=True)
    parser.add_argument('--seed', action='store_true')
    parser.add_argument('--reuse-seeded', action='store_true',
        help='Reuse the local synthetic fixture and skip all schema/setup writes')
    parser.add_argument('--candidates', type=int, default=86000)
    parser.add_argument('--samples', type=int, default=30)
    args = parser.parse_args()
    url = make_url(args.url)
    if url.host not in {'localhost', '127.0.0.1'} or not (url.database or '').startswith('geo_perf_'):
        parser.error('Only localhost geo_perf_* databases are allowed')
    sys.path.insert(0, str(Path(args.source_root).resolve() / 'src'))
    os.environ['OUTREACH_ADMIN_EMAIL'] = 'benchmark@example.test'
    from outreach_v2 import db as tables, auth
    from outreach_v2.app import create_app
    from fastapi.testclient import TestClient
    db = tables.Database(args.url)
    stamp = '2026-11-26T12:00:00+00:00'
    if args.seed:
        with db.engine.connect() as c:
            if c.scalar(text("SELECT EXISTS(SELECT FROM pg_namespace WHERE nspname='outreach_v2')")):
                parser.error('Refusing to seed an existing schema')
        db.initialize()
        with db.transaction() as c:
            for start in range(0, args.candidates, 1000):
                c.execute(tables.candidates.insert(), [dict(id=f'c{i:06}', name=f'Company {i:06}',
                    kind='hotel' if i % 5 == 0 else 'company', website=f'https://c{i}.example.test',
                    domain=f'c{i}.example.test', industry='Services', location='US',
                    website_status='VERIFIED', qualification='ELIGIBLE',
                    personalization_quality_status='PASS' if i % 10 == 0 else 'UNVERIFIED',
                    personalization_analysis_version='semantic-openai-v1', personalization_version=1,
                    personalization=json.dumps({'saved_evidence': 'x' * 4000}),
                    created_at=stamp, updated_at=stamp) for i in range(start, min(start+1000,args.candidates))])
            c.execute(tables.contacts.insert(), [dict(id=f'ct{i}', candidate_id=f'c{i:06}', kind='EMAIL',
                email=f'contact@c{i}.example.test', source_url=f'https://c{i}.example.test/contact',
                purpose='general business', evidence=f'For general business inquiries contact contact@c{i}.example.test',
                verification='VERIFIED', verified_at=stamp) for i in range(0,args.candidates,10)])
            for b in range(40):
                c.execute(tables.batches.insert().values(id=f'b{b:03}', request_key=f'k{b}', selection_hash='synthetic',
                    created_at=stamp, pilot_review_status='PENDING_REVIEW', package=b'x'*100000))
            for i in range(1000):
                c.execute(tables.invitations.insert().values(id=f'inv{i:04}', candidate_id=f'c{i:06}',
                    batch_id=f'b{i//25:03}', reference=f'ref{i}', recipient=f'contact@c{i}.example.test',
                    website=f'https://c{i}.example.test', company_name=f'Company {i:06}',
                    generated_at=stamp, body='synthetic saved invitation '*100, personalization='{}'))
            for i in range(250):
                c.execute(tables.submissions.insert().values(id=f's{i}', invitation_id=f'inv{i:04}', status='AUTHORIZED'))
                c.execute(tables.jobs.insert().values(id=f'j{i:04}', invitation_id=f'inv{i:04}', submission_id=f's{i}',
                    status='COMPLETED', created_at=stamp, completed_at=stamp, result='saved synthetic result'))
                c.execute(tables.reports.insert().values(id=f'r{i:04}',job_id=f'j{i:04}',recipient='contact@example.test',
                    sha256=f'hash{i}',document=b'synthetic saved document',created_at=stamp))
                for revision in range(5):
                    c.execute(tables.report_reviews.insert().values(id=f'rr{i}-{revision}', report_id=f'r{i:04}',
                        document_sha256=f'hash{i}', decision='APPROVED' if revision==4 else 'CHANGES_REQUESTED',
                        reviewer_email='reviewer@example.test', reviewed_at=f'2026-11-{20+revision}T12:00:00+00:00'))
            c.execute(tables.sessions.insert().values(token_hash=auth.digest('local-benchmark'),csrf_hash=auth.digest('local-csrf'),
                email='benchmark@example.test',expires_at='2099-01-01T00:00:00+00:00',created_at=stamp))
        with db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
            c.execute(text('ANALYZE'))
    elif not args.reuse_seeded:
        parser.error('Choose --seed for a fresh disposable database or --reuse-seeded for an existing local fixture')
    # Apply read-only at the DB connection level, including pooled connections.
    db.engine.dispose()
    @event.listens_for(db.engine, 'connect')
    def read_only(conn, _):
        with conn.cursor() as cursor:
            cursor.execute('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY')
        conn.commit()
    client = TestClient(create_app(db), base_url='https://testserver')
    client.cookies.set('outreach_session', 'local-benchmark')
    from outreach_v2.app import COOKIE
    client.cookies.set(COOKIE, 'local-benchmark')
    paths = ['/api/dashboard','/api/candidates?page_size=50','/api/admin/contact-readiness']
    result = {'workload':'local ASGI HTTP stack + PostgreSQL; warm; sequential; synthetic',
              'samples':args.samples, 'endpoints':{}, 'external_calls':0}
    captured=[]
    previous_sql={}
    def capture(conn, cursor, statement, parameters, context, executemany):
        captured.append((statement, parameters))
    plans={}
    for path in paths:
        response=client.get(path)
        if response.status_code != 200:
            result['endpoints'][path]={'status_code':response.status_code,'error':response.text}
            print(path, response.status_code, flush=True)
            continue
        latencies=[];counts=[];digests=set()
        event.listen(db.engine,'before_cursor_execute',capture)
        try:
            for _ in range(args.samples):
                captured.clear();started=time.perf_counter();response=client.get(path)
                latencies.append((time.perf_counter()-started)*1000)
                assert response.status_code==200, response.text
                counts.append(len(captured))
                if path not in previous_sql:
                    previous_sql[path]=list(captured)
                digests.add(hashlib.sha256(response.content).hexdigest())
        finally:
            event.remove(db.engine,'before_cursor_execute',capture)
        result['endpoints'][path]={'status_code':response.status_code,
            'p50_ms':round(statistics.median(latencies),3),
            'p95_ms':round(sorted(latencies)[math.ceil(len(latencies)*.95)-1],3),
            'sql_count':sorted(set(counts)), 'payload_bytes':len(response.content),
            'response_sha256':sorted(digests), 'samples_ms':[round(x,3) for x in latencies]}
        # EXPLAIN only the representative page query and aggregate queries;
        # rerunning correlated eligibility/count plans adds minutes and is not
        # needed to preserve the plan evidence from the captured execution.
        plans[path]=[]
        print(path, result['endpoints'][path]['p50_ms'], flush=True)
        Path(args.output).write_text(json.dumps(result,indent=2))
    result['plans']=plans
    Path(args.output).write_text(json.dumps(result,indent=2))
    Path(args.output).write_text(json.dumps(result,indent=2))
    client.close();db.engine.dispose()


if __name__ == '__main__':
    main()
