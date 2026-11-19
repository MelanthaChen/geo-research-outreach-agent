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
    from outreach_v2.db import Database, candidates, contacts, now
    from outreach_v2.migrate import dump
    from outreach_v2.service import generate, list_candidates, preview_generation

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
    try:
        batch=generate(db,selection,f'perf-{args.candidates}-{args.eligible}-{int(time.time())}')
    finally:
        elapsed_ms=(time.perf_counter()-start)*1000
        event.remove(db.engine,'before_cursor_execute',capture)
    print(json.dumps({'database':str(db_path),'candidates':args.candidates,'eligible':args.eligible,
        'list_page_size':50,'list_samples':args.samples,'list_sql_per_request':list_sql_per_request,
        'list_median_ms':round(statistics.median(latencies),2),'list_p95_ms':round(percentile(latencies,.95),2),
        'list_min_ms':round(min(latencies),2),'list_max_ms':round(max(latencies),2),
        'generation_sql_statements':len(statements),'generation_elapsed_ms':round(elapsed_ms,2),
        'generation_package_bytes':len(batch['package']),'review_status':batch['pilot_review_status'],
        'external_provider_calls':0,'emails_sent':0},indent=2))


if __name__=='__main__':
    main()
