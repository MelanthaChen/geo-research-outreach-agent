#!/usr/bin/env python3
"""Repeatable, local-only candidate-list and invitation-generation benchmark.

The script creates a disposable SQLite database and never calls external APIs,
loads live data, sends email, or touches the staging database.
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path


def percentile(values, fraction):
    ordered=sorted(values)
    return ordered[max(0, math.ceil(fraction*len(ordered))-1)]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-root', required=True, help='Checkout whose src/ code is measured')
    parser.add_argument('--db', required=True, help='New disposable SQLite path; existing files are refused')
    parser.add_argument('--candidates', type=int, default=86000)
    parser.add_argument('--eligible', type=int, default=150)
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--async-chunks', action='store_true',
        help='Benchmark durable enqueue and chunked processing')
    parser.add_argument('--reopen-between-ticks',action='store_true',
        help='Reopen the database connection between worker ticks to include restart recovery overhead')
    args=parser.parse_args()
    db_path=Path(args.db).resolve()
    if db_path.exists():
        raise SystemExit(f'Refusing to overwrite existing benchmark database: {db_path}')
    if args.candidates<args.eligible or args.eligible<1:
        raise SystemExit('--candidates must be >= --eligible >= 1')
    sys.path.insert(0,str(Path(args.source_root).resolve()/'src'))
    os.environ.setdefault('OUTREACH_RESEARCHER_NAME','Performance Test')
    os.environ.setdefault('OUTREACH_UNIVERSITY_AFFILIATION','Test University')
    os.environ.setdefault('OUTREACH_REPLY_TO','researcher@example.edu')

    from sqlalchemy import event
    from outreach_v2.db import Database, candidates, contacts, invitations, batches, now
    from outreach_v2.migrate import dump
    from outreach_v2.service import generate, list_candidates, preview_generation
    if args.async_chunks:
        from outreach_v2.invitation_generation import enqueue, process_one

    db=Database('sqlite:///'+str(db_path))
    db.initialize()
    stamp=now()
    excerpt='Product pages describe an online workflow, its users, and available features.'
    observed={
        'data_classification':'OBSERVED','analysis_version':'structured-evidence-v2',
        'pipeline_version':'semantic-openai-v1','business_description':excerpt,
        'company_strength':'The pages describe a digital product and its workflow.',
        'recipient_copy':{'business_summary':'The pages describe an online product and its workflow.',
            'distinctive_detail':'The pages explain how users navigate the product.',
            'research_relevance':'This is useful for a bounded study of how AI search summarizes product information.',
            'supported_observation':'','support':{'business_summary':['main'],'distinctive_detail':['main'],
                'research_relevance':['main']}},
        'claim_support':{'business_description':['main'],'company_strength':['main']},
        'evidence_profile':{'pages':[{'url':'https://bench-00000.example.com','type':'home_or_other',
            'title':'Benchmark company','headings':[],'truncated':False}],
            'facts':[{'id':'main','statement':excerpt,'evidence_excerpt':excerpt,
                'source_urls':['https://bench-00000.example.com'],
                'assertion_type':'FIRST_PARTY_PAGE_CLAIM','attribution':'COMPANY_STATED'}]},
        'source_urls':['https://bench-00000.example.com'],'retrieved_at':stamp,
        'evidence':{'visible_text_excerpt':excerpt},'recommendations':[],
        'assessment':{'geo_observation':'NOT_ESTABLISHED'}}
    candidate_rows=[]
    for index in range(args.candidates):
        ident=f'bench-{index:06d}'
        ready=index<args.eligible
        personalization=dict(observed)
        personalization['evidence_profile']=dict(observed['evidence_profile'])
        personalization['evidence_profile']['pages']=[{'url':f'https://{ident}.example.com',
            'type':'home_or_other','title':ident,'headings':[],'truncated':False}]
        personalization['evidence_profile']['facts']=[{'id':'main','statement':excerpt,
            'evidence_excerpt':excerpt,'source_urls':[f'https://{ident}.example.com'],
            'assertion_type':'FIRST_PARTY_PAGE_CLAIM','attribution':'COMPANY_STATED'}]
        personalization['source_urls']=[f'https://{ident}.example.com']
        candidate_rows.append({'id':ident,'kind':'company','name':f'Benchmark {index:06d}',
            'website':f'https://{ident}.example.com','domain':f'{ident}.example.com','industry':'Software',
            'location':'US','website_status':'VERIFIED' if ready else 'UNKNOWN',
            'qualification':'ELIGIBLE' if ready else 'UNKNOWN','geography':'US','suppressed':False,
            'source':'synthetic-performance-fixture','personalization':dump(personalization) if ready else '{}',
            'personalization_status':'READY' if ready else 'INCOMPLETE',
            'personalization_quality_status':'PASS' if ready else 'UNVERIFIED',
            'personalization_quality_errors':'[]','personalization_version':1 if ready else 0,
            'personalization_analysis_version':'semantic-openai-v1' if ready else None,'fixture':False,
            'created_at':stamp,'updated_at':stamp})
    with db.transaction() as connection:
        for start in range(0,len(candidate_rows),1000):
            connection.execute(candidates.insert(),candidate_rows[start:start+1000])
        contact_rows=[{'id':f'contact-{i:06d}','candidate_id':f'bench-{i:06d}','kind':'EMAIL',
            'email':f'bench-{i:06d}@example.com','source_url':f'https://bench-{i:06d}.example.com',
            'evidence':'Synthetic first-party benchmark evidence','verification':'VERIFIED','suppressed':False}
            for i in range(args.eligible)]
        connection.execute(contacts.insert(),contact_rows)

    selection={'ids':[f'bench-{i:06d}' for i in range(args.eligible)]}
    selection['preview_sha256']=preview_generation(db,selection)['preview_sha256']
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    latencies=[]
    event.listen(db.engine,'before_cursor_execute',capture)
    try:
        for _ in range(args.samples):
            start=time.perf_counter()
            list_candidates(db,{},page=1,page_size=50)
            latencies.append((time.perf_counter()-start)*1000)
    finally:
        event.remove(db.engine,'before_cursor_execute',capture)
    list_sql_per_request=len(statements)//args.samples

    statements=[]
    event.listen(db.engine,'before_cursor_execute',capture)
    start=time.perf_counter()
    if args.async_chunks:
        generation=enqueue(db,selection,f'perf-async-{args.candidates}-{args.eligible}-{int(time.time())}')
        enqueue_ms=(time.perf_counter()-start)*1000
        enqueue_statements=len(statements)
        event.remove(db.engine,'before_cursor_execute',capture)
        if args.reopen_between_ticks:
            db.engine.dispose()
            db=Database('sqlite:///'+str(db_path));db.initialize()
        chunk_latencies=[];chunk_statements=[];tick_latencies=[];chunk_count=0
        while True:
            tick_start=time.perf_counter();finished=False
            for _ in range(4):
                event.listen(db.engine,'before_cursor_execute',capture)
                chunk_start=time.perf_counter()
                result=process_one(db,chunk_size=25)
                chunk_latencies.append((time.perf_counter()-chunk_start)*1000)
                chunk_statements.append(len(statements)-sum(chunk_statements)-enqueue_statements)
                event.remove(db.engine,'before_cursor_execute',capture)
                if result is None:
                    finished=True;break
                chunk_count+=1
                if result['status']!='PROCESSING':
                    finished=True;break
            tick_latencies.append((time.perf_counter()-tick_start)*1000)
            if finished:break
            if args.reopen_between_ticks:
                db.engine.dispose()
                db=Database('sqlite:///'+str(db_path));db.initialize()
        with db.transaction() as c:
            from sqlalchemy import select,func
            from outreach_v2.db import invitation_generation_batches
            invitation_count=c.scalar(select(func.count()).select_from(invitations).where(invitations.c.batch_id==generation['batch_id']))
            batch_state=c.execute(select(invitation_generation_batches.c.status).where(
                invitation_generation_batches.c.batch_id==generation['batch_id'])).scalar_one()
            package=c.execute(select(batches.c.package).where(batches.c.id==generation['batch_id'])).scalar_one()
        batch={'package':package,'pilot_review_status':'PENDING_REVIEW'}
        elapsed_ms=enqueue_ms+sum(tick_latencies)
        generation_sql_statements=enqueue_statements+sum(chunk_statements)
    else:
        try:
            batch=generate(db,selection,f'perf-{args.candidates}-{args.eligible}-{int(time.time())}')
            elapsed_ms=(time.perf_counter()-start)*1000
            generation_sql_statements=len(statements)
        finally:
            event.remove(db.engine,'before_cursor_execute',capture)
    output={'database':str(db_path),'candidates':args.candidates,'eligible':args.eligible,
        'list_page_size':50,'list_samples':args.samples,'list_sql_per_request':list_sql_per_request,
        'list_median_ms':round(statistics.median(latencies),2),'list_p95_ms':round(percentile(latencies,.95),2),
        'list_min_ms':round(min(latencies),2),'list_max_ms':round(max(latencies),2),
        'generation_mode':'durable_chunked' if args.async_chunks else 'synchronous_reference',
        'generation_sql_statements':generation_sql_statements,'generation_elapsed_ms':round(elapsed_ms,2),
        'generation_package_bytes':len(batch['package']),'review_status':batch['pilot_review_status'],
        'external_provider_calls':0,'emails_sent':0}
    if args.async_chunks:
        output.update({'enqueue_response_ms':round(enqueue_ms,2),'enqueue_sql_statements':enqueue_statements,
            'worker_chunks':chunk_count,'worker_chunk_size':25,
            'chunks_per_worker_tick':4,'worker_ticks':len(tick_latencies),
            'worker_tick_median_ms':round(statistics.median(tick_latencies),2),
            'worker_tick_p95_ms':round(percentile(tick_latencies,.95),2),
            'worker_chunk_median_ms':round(statistics.median(chunk_latencies),2),
            'worker_chunk_p95_ms':round(percentile(chunk_latencies,.95),2),
            'database_reopened_between_worker_ticks':args.reopen_between_ticks,'persisted_generation_status':batch_state,
            'persisted_invitation_count':invitation_count})
    print(json.dumps(output,indent=2))


if __name__=='__main__':
    main()
