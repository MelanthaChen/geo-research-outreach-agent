"""Read-only audit of existing READY records and exported invitations."""
from __future__ import annotations
import argparse, json, os
from sqlalchemy import select
from .db import candidates, invitations, batches, SCHEMA
from .migrate import parse
from .quality import quality_reasons, CODE_MARKERS

def audit(db):
    # Refuse to run through Database.initialize(): this audit must never create
    # tables or change the existing operational namespace.
    with db.engine.connect() as c:
        if db.postgres:
            c.exec_driver_sql('SET TRANSACTION READ ONLY')
        ready=c.execute(select(candidates.c.id,candidates.c.name,candidates.c.website,candidates.c.personalization)
            .where(candidates.c.personalization_status=='READY')).mappings().all()
        exported=c.execute(select(invitations.c.id,invitations.c.company_name,invitations.c.personalization,
            invitations.c.body,invitations.c.batch_id,batches.c.send_status).select_from(invitations.join(
            batches,invitations.c.batch_id==batches.c.id)).where(invitations.c.exported_at.is_not(None))).mappings().all()
        c.rollback()
    def inspect(rows, invitation=False):
        bad=[]
        for row in rows:
            p=parse(row['personalization'])
            failures=quality_reasons(p)
            try:
                from .template import validate
                validate(p,row.get('website'))
            except (ValueError,TypeError):
                failures=list(dict.fromkeys([*failures,'INSUFFICIENT_EVIDENCE']))
            desc=str(p.get('business_description') or '')
            body=str(row.get('body') or '')
            if invitation and (CODE_MARKERS.search(body) or CODE_MARKERS.search(desc)):
                failures=list(dict.fromkeys([*failures,'CODE_CONTAMINATION']))
            if failures:
                # Names and failure codes only: do not return recipients, full
                # invitation bodies, or raw source snippets from this audit.
                bad.append({'record_id':row['id'],'company':row.get('company_name') or row.get('name'),
                    'reasons':failures,'send_status':row.get('send_status')})
        return bad
    ready_bad=inspect(ready)
    export_bad=inspect(exported,True)
    return {'ready_records':len(ready),'ready_quality_failures':len(ready_bad),
        'exported_invitations':len(exported),'exported_quality_failures':len(export_bad),
        'ready_examples':ready_bad[:20],'exported_examples':export_bad[:20],
        'historical_rows_modified':0}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url',default=os.getenv('OUTREACH_V2_DATABASE_URL') or os.getenv('OUTREACH_DATABASE_URL'))
    args=parser.parse_args()
    if not args.database_url: raise SystemExit('No existing database URL configured; no data was read or changed.')
    url=args.database_url
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    if url.startswith('sqlite:///'):
        parsed=make_url(url);query=dict(parsed.query);query.update({'mode':'ro','uri':'true'})
        url=parsed.set(query=query).render_as_string(hide_password=False)
    db=type('ReadOnlyAuditDatabase',(),{})()
    db.postgres=url.startswith(('postgresql://','postgres://','postgresql+'))
    db.engine=create_engine(url,execution_options={'schema_translate_map':{} if db.postgres else {SCHEMA:None}},pool_pre_ping=True)
    print(json.dumps(audit(db),indent=2,ensure_ascii=False))

if __name__=='__main__': main()
