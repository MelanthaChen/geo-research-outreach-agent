"""One durable scheduler tick. No browser timer or web-process background loop."""
from __future__ import annotations
import hashlib,json,os,tempfile,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import select,or_,and_
from .db import jobs,invitations,submissions,candidates,reports,deliveries,worker_runs,events,now
from .migrate import dump,parse,sha
from .consent import process
from .integrations import Sheets,Engine,GraphReports,ConfigurationBlocked,OutcomeUncertain
from .template import same_site

def later(seconds):return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
def transition(c,job_id,**values):
    c.execute(jobs.update().where(jobs.c.id==job_id).values(updated_at=now(),**values))
    c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=job_id,event=values.get('status') or values.get('stage') or 'UPDATED',detail=dump({k:v for k,v in values.items() if k not in {'result','lease_token'}}),created_at=now()))

def claim(db):
    with db.transaction(serialized=True) as c:
        row=c.execute(select(jobs).where(jobs.c.stage!='DONE',jobs.c.status.not_in(['BLOCKED_CONSENT','FAILED']),
                or_(jobs.c.next_attempt_at.is_(None),jobs.c.next_attempt_at<=now()),
                or_(jobs.c.lease_until.is_(None),jobs.c.lease_until<now())).order_by(jobs.c.created_at).limit(1)).mappings().first()
        if not row:return None
        token=uuid.uuid4().hex
        c.execute(jobs.update().where(jobs.c.id==row['id']).values(lease_until=later(180),lease_token=token,attempts=row['attempts']+1))
        return dict(row)|{'lease_token':token,'attempts':row['attempts']+1}

def context(c,job):
    current=c.execute(select(jobs).where(jobs.c.id==job['id'])).mappings().one()
    if current['lease_token']!=job['lease_token']:raise OutcomeUncertain('Worker lease has been superseded')
    inv=c.execute(select(invitations).where(invitations.c.id==job['invitation_id'])).mappings().one()
    candidate=c.execute(select(candidates).where(candidates.c.id==inv['candidate_id'])).mappings().one()
    if inv['consent_status']!='AUTHORIZED' or candidate['suppressed']:
        raise PermissionError('Research or delivery blocked by withdrawn/conflicting consent')
    return dict(inv),dict(candidate)

def validate_result(result,inv,job):
    if result.get('source_profile_id')!=inv['candidate_id'] or result.get('source_profile_version')!=inv['id']:
        raise ValueError('Result identity/version does not match this invitation')
    if str(result.get('property_id'))!=str(job.get('property_id')):raise ValueError('Result property mismatch')
    if not result.get('record_id') or result.get('contract_version')!='geo-research-integration/v1':raise ValueError('Missing real result identifier/contract')
    pages=result.get('observed_pages') or [];baseline=result.get('baseline') or {}
    if not pages or any(not same_site(inv['website'],p.get('url')) for p in pages):raise ValueError('Result evidence is missing or outside consented website scope')
    if not result.get('recommendations') or not baseline.get('results') or str(baseline.get('provider','')).lower() in {'','none','mock','controlled_fixture'} or int(baseline.get('model_calls') or 0)<1:
        raise ValueError('Result is not completed evidence-based model research')
    if not baseline.get('model') or not baseline.get('response_archive'):raise ValueError('Result lacks real model execution provenance')

def research_step(db,job,engine_factory):
    with db.transaction() as c:inv,candidate=context(c,job)
    engine=engine_factory()
    result=engine.reconcile(job['idempotency_key'])
    if result is None:
        payload={'profile_id':inv['candidate_id'],'profile_version':inv['id'],
            'profile_type':'owner_controlled_test' if candidate['fixture'] else ('hotel_property' if candidate['kind']=='hotel' else 'business_company'),
            'identity':{'name':inv['company_name'],'website':inv['website'],'industry':candidate['industry'],'location':candidate['location']},
            'consent':{'status':'VERIFIED','submission_id':job['submission_id'],'invitation_reference':inv['reference'],'scope_website':inv['website'],'research_interest':'YES','research_authorization':'YES'},
            'controlled_fixture':False}
        result=engine.start(job['idempotency_key'],payload)
    status=result.get('status')
    if status not in {'QUEUED','RUNNING','COMPLETED','FAILED','BLOCKED_CONFIGURATION','RETRY_PENDING'}:raise ValueError('Unknown Engine job status')
    values={'status':status,'engine_job_id':str(result.get('job_id') or ''),'property_id':str(result.get('property_id') or '') or None,
            'audit_id':str(result.get('audit_id') or '') or None,'opportunity_ids':dump(result.get('opportunity_ids') or []),
            'error':result.get('error'),'next_attempt_at':later(120),'started_at':job['started_at'] or now()}
    if status=='COMPLETED':
        envelope=result.get('result') or {};validate_result(envelope,inv,job|values)
        values.update(result=dump(envelope),result_id=envelope['record_id'],stage='REPORT',completed_at=now(),next_attempt_at=now())
    with db.transaction(serialized=True) as c:
        context(c,job);transition(c,job['id'],**values)

def report_step(db,job,renderer=None):
    with db.transaction() as c:
        inv,_=context(c,job)
        existing=c.execute(select(reports).where(reports.c.job_id==job['id'])).mappings().first()
    if not existing:
        result=parse(job['result']);validate_result(result,inv,job)
        if renderer:document=renderer(result)
        else:
            from outreach_agent.reporting import generate_docx_report
            template=Path(os.environ.get('OUTREACH_REPORT_TEMPLATE','templates/company_geo_analysis_report_template.docx'))
            if not template.exists():raise ConfigurationBlocked('Existing report template is unavailable')
            with tempfile.TemporaryDirectory() as temp:
                output=Path(temp)/'report.docx';generate_docx_report(result,output,template);document=output.read_bytes()
        with db.transaction(serialized=True) as c:
            context(c,job)
            report_id='report-'+sha(job['id'])[:24]
            db.upsert(c,reports,[dict(id=report_id,job_id=job['id'],recipient=inv['recipient'],result_id=result['record_id'],document=document,sha256=hashlib.sha256(document).hexdigest(),created_at=now())],update=False)
    with db.transaction(serialized=True) as c:
        context(c,job);transition(c,job['id'],stage='DELIVERY',status='COMPLETED',error=None,next_attempt_at=now())

def delivery_step(db,job,mail_factory):
    with db.transaction() as c:
        inv,_=context(c,job);report=dict(c.execute(select(reports).where(reports.c.job_id==job['id'])).mappings().one())
        if report['recipient']!=inv['recipient']:raise ValueError('Report recipient does not match original invitation')
    mail=mail_factory()  # Authorization is required before any provider action.
    key='outreach-report:'+report['id']
    with db.transaction(serialized=True) as c:
        context(c,job)
        db.upsert(c,deliveries,[dict(id='delivery-'+sha(key)[:24],report_id=report['id'],recipient=inv['recipient'],provider='MICROSOFT_GRAPH',idempotency_key=key,status='NOT_STARTED',provider_message_id=None,attempted_at=None,accepted_at=None,delivered_at=None,error=None,next_attempt_at=None,attempts=0)],update=False)
        delivery=dict(c.execute(select(deliveries).where(deliveries.c.report_id==report['id'])).mappings().one())
        if delivery['status']=='ACCEPTED':
            transition(c,job['id'],stage='DONE',status='COMPLETED',error=None);return
    provider=mail.reconcile(key)
    if provider:
        recipients=[r.get('emailAddress',{}).get('address','').lower() for r in provider.get('toRecipients',[])]
        if recipients!=[inv['recipient'].lower()]:raise ValueError('Provider message recipient differs from invitation')
        if not provider.get('isDraft',True):
            with db.transaction(serialized=True) as c:
                context(c,job)
                c.execute(deliveries.update().where(deliveries.c.id==delivery['id']).values(status='ACCEPTED',provider_message_id=provider['id'],accepted_at=provider.get('sentDateTime') or now(),error=None))
                transition(c,job['id'],stage='DONE',status='COMPLETED',error=None)
            return
    if delivery['status'] in {'CREATING','SENDING','UNCERTAIN'}:
        # A provider timeout is not proof that nothing was sent. Reconcile until an actual outcome is visible.
        if not provider or delivery['status'] in {'SENDING','UNCERTAIN'}:raise OutcomeUncertain('Provider outcome unresolved; automatic duplicate send prevented')
    if not provider:
        with db.transaction(serialized=True) as c:
            context(c,job);c.execute(deliveries.update().where(deliveries.c.id==delivery['id']).values(status='CREATING',attempted_at=now(),attempts=delivery['attempts']+1))
        provider=mail.create(key,inv['recipient'],report['document'])
    with db.transaction(serialized=True) as c:
        context(c,job);c.execute(deliveries.update().where(deliveries.c.id==delivery['id']).values(status='SENDING',provider_message_id=provider['id']))
    outcome=mail.send(provider['id'])
    if outcome.get('status')!='ACCEPTED':raise OutcomeUncertain('Provider did not acknowledge report acceptance')
    with db.transaction(serialized=True) as c:
        # Record true provider acceptance even if consent changed while the request was in flight.
        c.execute(deliveries.update().where(deliveries.c.id==delivery['id']).values(status='ACCEPTED',provider_message_id=provider['id'],accepted_at=now(),error=None))
        transition(c,job['id'],stage='DONE',status='COMPLETED',error=None)

def tick(db,*,sheets=None,engine_factory=Engine,mail_factory=GraphReports,renderer=None,max_jobs=20):
    run_id=uuid.uuid4().hex;errors=[];processed=0
    with db.transaction() as c:c.execute(worker_runs.insert().values(id=run_id,started_at=now(),status='RUNNING'))
    try:
        for row in (sheets or Sheets()).read():process(db,row)
    except Exception as e:errors.append('Sheets: '+safe_error(e))
    for _ in range(max_jobs):
        job=claim(db)
        if not job:break
        try:
            if job['stage']=='RESEARCH':research_step(db,job,engine_factory)
            elif job['stage']=='REPORT':report_step(db,job,renderer)
            elif job['stage']=='DELIVERY':delivery_step(db,job,mail_factory)
            else:raise ValueError('Unknown durable job stage')
            processed+=1
        except Exception as e:
            status='BLOCKED_CONSENT' if isinstance(e,PermissionError) else 'BLOCKED_CONFIGURATION' if isinstance(e,ConfigurationBlocked) else 'FAILED' if isinstance(e,ValueError) else 'RETRY_PENDING'
            error=safe_error(e);errors.append(job['id']+': '+error)
            with db.transaction(serialized=True) as c:
                row=c.execute(select(jobs.c.lease_token).where(jobs.c.id==job['id'])).first()
                if row and row.lease_token==job['lease_token']:
                    transition(c,job['id'],status=status,error=error,next_attempt_at=later(min(3600,60*2**min(job['attempts'],6))))
        finally:
            with db.transaction() as c:c.execute(jobs.update().where(jobs.c.id==job['id'],jobs.c.lease_token==job['lease_token']).values(lease_until=None,lease_token=None))
    result={'status':'COMPLETED' if not errors else 'ATTENTION_REQUIRED','processed_stages':processed,'errors':errors}
    with db.transaction() as c:c.execute(worker_runs.update().where(worker_runs.c.id==run_id).values(finished_at=now(),status=result['status'],detail=dump(result)))
    return result

def safe_error(exc):
    # Only locally authored validation errors are safe to persist. HTTP and DB exceptions can contain secrets.
    if isinstance(exc,(ConfigurationBlocked,OutcomeUncertain,PermissionError,ValueError)):
        return str(exc)[:500]
    return type(exc).__name__+'; operation failed and remains recoverable'
