"""One durable scheduler tick. No browser timer or web-process background loop."""
from __future__ import annotations
import hashlib,hmac,json,os,tempfile,uuid
import re,zipfile
from io import BytesIO
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import select,or_,and_
from .db import jobs,invitations,submissions,candidates,reports,deliveries,worker_runs,events,report_reviews,admin_users,email_templates,now
from .migrate import dump,parse,sha
from .consent import process
from .integrations import Sheets,Engine,GraphReports,ConfigurationBlocked,OutcomeUncertain,VerifiedFormResponse
from .template import same_site
from outreach_agent.google_forms_integration import parse_sheet_row

def later(seconds):return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
def transition(c,job_id,**values):
    c.execute(jobs.update().where(jobs.c.id==job_id).values(updated_at=now(),**values))
    c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=job_id,event=values.get('status') or values.get('stage') or 'UPDATED',detail=dump({k:v for k,v in values.items() if k not in {'result','lease_token'}}),created_at=now()))

def claim(db,*,job_id=None,idempotency_key=None,stage=None,scheduled=False,controlled=False):
    research_only=os.environ.get('GEO_OUTREACH_RESEARCH_ENABLED','').strip().lower()=='true'
    delivery_enabled=os.environ.get('OUTREACH_REPORT_DELIVERY_ENABLED','').strip().lower()=='true'
    # A live single-job research switch must constrain selection itself. A
    # downstream research_step guard is too late: claiming another queued job
    # mutates its lease/attempt count even when that guard blocks Engine access.
    allowed_keys={x.strip() for x in os.environ.get('GEO_OUTREACH_RESEARCH_JOB_ALLOWLIST','').split(',') if x.strip()}
    if (research_only or scheduled) and len(allowed_keys)!=1:return None
    if (job_id or idempotency_key) and stage not in {'RESEARCH','REPORT'}:
        raise ValueError('Exact-job worker invocation must select RESEARCH or REPORT stage')
    # A scheduled tick may ingest all fresh Forms responses, but it may only
    # claim the one job explicitly named by the staging allowlist.  The
    # research and delivery switches gate those side-effecting stages before
    # a lease or attempt counter is changed.
    if job_id or idempotency_key:
        if controlled and stage=='RESEARCH' and not research_only:return None
        if stage=='DELIVERY' and not delivery_enabled:return None
    elif scheduled and len(allowed_keys)!=1:return None
    if job_id and idempotency_key:
        with db.transaction() as c:
            if not c.scalar(select(jobs.c.id).where(jobs.c.id==job_id,jobs.c.idempotency_key==idempotency_key)):
                return None
    with db.transaction(serialized=True) as c:
        eligible=[jobs.c.stage!='DONE',jobs.c.status.not_in(['BLOCKED_CONSENT','FAILED']),
                or_(jobs.c.next_attempt_at.is_(None),jobs.c.next_attempt_at<=now()),
                or_(jobs.c.lease_until.is_(None),jobs.c.lease_until<now())]
        if scheduled or research_only:
            eligible.append(jobs.c.idempotency_key.in_(allowed_keys))
        if scheduled and not research_only:
            eligible.append(jobs.c.stage!='RESEARCH')
        if scheduled and not delivery_enabled:
            eligible.append(jobs.c.stage!='DELIVERY')
        if job_id:eligible.append(jobs.c.id==job_id)
        if idempotency_key:eligible.append(jobs.c.idempotency_key==idempotency_key)
        if stage:eligible.append(jobs.c.stage==stage)
        row=c.execute(select(jobs).where(*eligible).order_by(jobs.c.created_at).limit(1)).mappings().first()
        if not row:return None
        token=uuid.uuid4().hex
        claimed_stage=row['stage']
        c.execute(jobs.update().where(jobs.c.id==row['id']).values(stage=claimed_stage,lease_until=later(180),lease_token=token,attempts=row['attempts']+1))
        return dict(row)|{'stage':claimed_stage,'lease_token':token,'attempts':row['attempts']+1}

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

_SIMULATION_MARKERS = (b'TEST-ONLY', b'TEST ONLY', b'NO-ENGINE-RESULT', b'NO ENGINE RESULT',
                       b'SIMULATED PREVIEW', b'SIMULATED RESULT', b'FIXTURE-GENERATED')

def _document_has_simulation_marker(document):
    if not document or not zipfile.is_zipfile(BytesIO(document)):
        return True
    try:
        with zipfile.ZipFile(BytesIO(document)) as archive:
            xml = archive.read('word/document.xml')
    except (KeyError, OSError, zipfile.BadZipFile):
        return True
    normalized = re.sub(rb'\s+', b' ', xml).upper()
    return any(marker in normalized for marker in _SIMULATION_MARKERS)

def _test_only_result(result):
    baseline=result.get('baseline') or {}
    result_id=str(result.get('record_id') or '').upper()
    provider=str(baseline.get('provider') or '').casefold()
    return (any(marker.decode('ascii') in result_id for marker in _SIMULATION_MARKERS)
            or bool(result.get('test_only') or result.get('simulated') or result.get('fixture_generated'))
            or provider in {'mock','controlled_fixture','fixture','simulated','test'})

def _valid_report_artifact(report, expected_result_id):
    document=report.get('document') or b''
    digest=hashlib.sha256(document).hexdigest()
    return (report.get('result_id')==expected_result_id
            and bool(document)
            and bool(report.get('sha256'))
            and hmac.compare_digest(digest,str(report.get('sha256')))
            and not _document_has_simulation_marker(document)
            and 'TEST-ONLY' not in str(report.get('result_id') or '').upper()
            and 'NO-ENGINE-RESULT' not in str(report.get('result_id') or '').upper())

def _genuine_job_result(job, inv):
    if job.get('status')!='COMPLETED' or job.get('stage') not in {'REPORT','DELIVERY'} or not job.get('result_id'):
        raise ConfigurationBlocked('A completed genuine research result is required before report generation')
    result=parse(job.get('result'))
    validate_result(result,inv,job)
    if result.get('record_id')!=job.get('result_id') or _test_only_result(result):
        raise ConfigurationBlocked('Research result identity or provenance is not genuine')
    return result

def _verified_research_submission(inv, submission):
    """Require the real verified Forms identity or a separately audited binding."""
    if (submission.get('status') != 'AUTHORIZED'
        or submission.get('identity_provider') != 'GOOGLE_FORMS_VERIFIED'
        or not str(submission.get('identity_reference') or '').strip()
        or not str(submission.get('identity_verified_email') or '').strip()):
        return False
    verified_email=str(submission['identity_verified_email']).strip().lower()
    recipient=str(inv.get('recipient') or '').strip().lower()
    representative=str(submission.get('representative_email') or '').strip().lower()
    if verified_email and verified_email == recipient == representative:
        return True
    authority=parse(inv.get('authority'))
    binding=authority.get('owner_identity_binding') or {}
    return bool(
        binding.get('id')
        and binding.get('authorization') == 'EXPLICIT_OWNER_CONTROLLED_STAGING_BINDING'
        and binding.get('identity_source') == 'GOOGLE_FORMS_VERIFIED_RESPONDENT_EMAIL'
        and binding.get('candidate_id') == inv.get('candidate_id')
        and binding.get('invitation_id') == inv.get('id')
        and binding.get('invitation_reference') == inv.get('reference')
        and str(binding.get('website') or '').rstrip('/') == str(inv.get('website') or '').rstrip('/')
        and str(binding.get('invitation_recipient') or '').strip().lower() == recipient
        and str(binding.get('verified_respondent_email') or '').strip().lower() == verified_email
        and bool(binding.get('recorded_at'))
    )

def research_step(db,job,engine_factory):
    with db.transaction() as c:inv,candidate=context(c,job)
    allowed_job_ids={x.strip() for x in os.environ.get('GEO_OUTREACH_RESEARCH_JOB_ALLOWLIST','').split(',') if x.strip()}
    if len(allowed_job_ids)!=1 or job['idempotency_key'] not in allowed_job_ids:
        raise ConfigurationBlocked('Research is restricted to the exact configured job allowlist')
    research_enabled=os.environ.get('GEO_OUTREACH_RESEARCH_ENABLED','').lower()=='true'
    with db.transaction() as c:
        submission=dict(c.execute(select(submissions).where(submissions.c.id==job['submission_id'])).mappings().one())
    if not _verified_research_submission(inv,submission):
        raise PermissionError('Research requires independently verified Google Forms identity, authorized consent, and a matching invitation or audited identity binding')
    binding=parse(inv.get('authority')).get('owner_identity_binding') or {}
    engine=engine_factory()
    # Reconciliation is a read-only GET. Always allow it so an Engine result
    # completed under its own authorization can advance to report generation.
    result=engine.reconcile(job['idempotency_key'])
    if result is None:
        if not research_enabled:
            raise ConfigurationBlocked('GEO research execution is disabled; no existing Engine job is available to reconcile')
        payload={'profile_id':inv['candidate_id'],'profile_version':inv['id'],'invitation_id':inv['id'],
            'invitation_recipient':inv['recipient'],
            'profile_type':'owner_controlled_test' if candidate['fixture'] else ('hotel_property' if candidate['kind']=='hotel' else 'business_company'),
            'identity':{'name':inv['company_name'],'website':inv['website'],'industry':candidate['industry'],'location':candidate['location']},
            'consent':{'status':'VERIFIED','consent_id':job['submission_id'],'version':'outreach-consent-v2','research_scope':'Read-only public website analysis and model-based research; no website modifications or monitoring','submission_id':job['submission_id'],'invitation_reference':inv['reference'],'scope_website':inv['website'],'research_interest':'YES','research_authorization':'YES','representative_email':submission.get('representative_email'),
                'verified_identity':({'provider':submission.get('identity_provider'),'email':submission.get('identity_verified_email'),'response_id':submission.get('identity_reference')} if submission else None),
                'owner_identity_binding':binding},
            }
        result=engine.start(job['idempotency_key'],payload)
    if research_enabled and result.get('status') in {'QUEUED','RUNNING','RETRY_PENDING','BLOCKED_CONFIGURATION'}:
        result=engine.advance(job['idempotency_key'])
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
        current=c.execute(select(jobs).where(jobs.c.id==job['id'])).mappings().one()
        result=_genuine_job_result(dict(current),inv)
        existing=c.execute(select(reports).where(reports.c.job_id==job['id'],reports.c.is_active.is_(True))).mappings().first()
        version=c.execute(select(reports).where(reports.c.result_id==result['record_id'])).mappings().first()
        if existing and existing['result_id']==result['record_id']:
            if not _valid_report_artifact(dict(existing),result['record_id']):
                raise ConfigurationBlocked('Matching report has invalid integrity or simulated provenance')
            document=None
        elif version:
            if not _valid_report_artifact(dict(version),result['record_id']):
                raise ConfigurationBlocked('Existing result version has invalid integrity or simulated provenance')
            document=None
        elif existing and not ('TEST-ONLY' in str(existing['result_id'] or '').upper()
                               or 'NO-ENGINE-RESULT' in str(existing['result_id'] or '').upper()
                               or _document_has_simulation_marker(existing['document'] or b'')):
            raise ConfigurationBlocked('An active report for a different genuine result cannot be replaced')
        else:
            document=False
    if document is False:
        if renderer:document=renderer(result)
        else:
            from outreach_agent.reporting import generate_docx_report
            template=Path(os.environ.get('OUTREACH_REPORT_TEMPLATE','templates/company_geo_analysis_report_template.docx'))
            if not template.exists():raise ConfigurationBlocked('Existing report template is unavailable')
            with tempfile.TemporaryDirectory() as temp:
                output=Path(temp)/'report.docx';generate_docx_report(result,output,template);document=output.read_bytes()
    if document is not None and _document_has_simulation_marker(document):
        raise ConfigurationBlocked('Generated DOCX is not a genuine research artifact')
    new_sha=hashlib.sha256(document).hexdigest() if document is not None else None
    new_id='report-'+sha(job['id']+':'+str(result['record_id']))[:24]
    with db.transaction(serialized=True) as c:
        inv,_=context(c,job)
        current=dict(c.execute(select(jobs).where(jobs.c.id==job['id'])).mappings().one())
        fresh_result=_genuine_job_result(current,inv)
        if fresh_result['record_id']!=result['record_id']:
            raise ConfigurationBlocked('Research result changed while the report was being rendered')
        existing=c.execute(select(reports).where(reports.c.job_id==job['id'],reports.c.is_active.is_(True))).mappings().first()
        version=c.execute(select(reports).where(reports.c.result_id==result['record_id'])).mappings().first()
        if version:
            version=dict(version)
            if not _valid_report_artifact(version,result['record_id']):
                raise ConfigurationBlocked('Existing result version has invalid integrity or simulated provenance')
            if existing and existing['id']!=version['id']:
                if not ('TEST-ONLY' in str(existing['result_id'] or '').upper()
                        or 'NO-ENGINE-RESULT' in str(existing['result_id'] or '').upper()
                        or _document_has_simulation_marker(existing['document'] or b'')):
                    raise ConfigurationBlocked('An active report for a different genuine result cannot be replaced')
                c.execute(reports.update().where(reports.c.id==existing['id'],reports.c.is_active.is_(True)).values(
                    is_active=False,superseded_at=now(),superseded_by=version['id']))
                c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=job['id'],event='REPORT_VERSION_SUPERSEDED',
                    detail=dump({'old_report_id':existing['id'],'old_result_id':existing['result_id'],'old_sha256':existing['sha256'],
                                 'new_report_id':version['id'],'new_result_id':result['record_id'],'new_sha256':version['sha256']}),created_at=now()))
            if not version['is_active']:
                c.execute(reports.update().where(reports.c.id==version['id']).values(is_active=True,superseded_at=None,superseded_by=None))
        elif document is not None:
            if existing:
                old=dict(existing)
                old_hash=hashlib.sha256(old['document'] or b'').hexdigest()
                if not old['document'] or not old['sha256'] or not hmac.compare_digest(old_hash,str(old['sha256'])):
                    raise ConfigurationBlocked('Existing preview integrity check failed; it cannot be safely superseded')
                if not ('TEST-ONLY' in str(old['result_id'] or '').upper()
                        or 'NO-ENGINE-RESULT' in str(old['result_id'] or '').upper()
                        or _document_has_simulation_marker(old['document'] or b'')):
                    raise ConfigurationBlocked('An active report for a different genuine result cannot be replaced')
                c.execute(reports.update().where(reports.c.id==old['id'],reports.c.is_active.is_(True)).values(
                    is_active=False,superseded_at=now(),superseded_by=new_id))
                c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=job['id'],event='REPORT_VERSION_SUPERSEDED',
                    detail=dump({'old_report_id':old['id'],'old_result_id':old['result_id'],'old_sha256':old['sha256'],
                                 'new_report_id':new_id,'new_result_id':result['record_id'],'new_sha256':new_sha}),created_at=now()))
            c.execute(reports.insert().values(id=new_id,job_id=job['id'],recipient=inv['recipient'],result_id=result['record_id'],
                document=document,sha256=new_sha,created_at=now(),is_active=True))
        transition(c,job['id'],stage='DELIVERY',status='COMPLETED',error=None,next_attempt_at=now())

def report_delivery_enabled():
    return os.environ.get('OUTREACH_REPORT_DELIVERY_ENABLED','false').strip().lower()=='true'

def delivery_step(db,job,mail_factory):
    with db.transaction() as c:
        inv,_=context(c,job);report=dict(c.execute(select(reports).where(reports.c.job_id==job['id'],reports.c.is_active.is_(True))).mappings().one())
        template=c.execute(select(email_templates).where(email_templates.c.active.is_(True),
            email_templates.c.template_kind=='REPORT').order_by(email_templates.c.version.desc()).limit(1)).mappings().first()
        if report['recipient']!=inv['recipient']:raise ValueError('Report recipient does not match original invitation')
        document_hash=hashlib.sha256(report['document'] or b'').hexdigest()
        if not report['document'] or not report['sha256'] or not hmac.compare_digest(document_hash,report['sha256']):
            raise ConfigurationBlocked('Stored DOCX integrity check failed; delivery is blocked')
        review=c.execute(select(report_reviews).where(report_reviews.c.report_id==report['id'],
            report_reviews.c.document_sha256==document_hash).order_by(report_reviews.c.reviewed_at.desc())).mappings().first()
        if not review or review['decision']!='APPROVED':
            raise ConfigurationBlocked('Exact DOCX version awaits approval from an authorized administrator')
        reviewer=review['reviewer_email'].lower()
        admin=c.execute(select(admin_users.c.role,admin_users.c.status).where(admin_users.c.email==reviewer)).mappings().first()
        env_admin=os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu').lower()
        authorized=bool(admin and admin['status']=='ACTIVE' and admin['role'] in {'ADMIN','APPROVER'}) or reviewer==env_admin
        if not authorized:
            raise ConfigurationBlocked('DOCX approval is no longer attributable to an active administrator')
        if not report_delivery_enabled():
            raise ConfigurationBlocked('Automatic report delivery is disabled for this controlled research run')
    mail=mail_factory()  # Authorization is required before any provider action.
    if template and hasattr(mail,'configure_message'):
        from .template import render_template
        with db.transaction() as c:
            context(c,job)
        values={'company_name':inv['company_name'],'recipient':inv['recipient'],'report_name':'GEO research report',
            'researcher_name':template['researcher_name'] or os.environ.get('OUTREACH_RESEARCHER_NAME',''),
            'university_affiliation':template['university_affiliation'] or os.environ.get('OUTREACH_UNIVERSITY_AFFILIATION',''),
            'reply_to':template['reply_to'] or os.environ.get('OUTREACH_REPLY_TO',''),
            'sender_name':template.get('sender_name') or os.environ.get('OUTREACH_RESEARCHER_NAME','')}
        mail.configure_message(render_template(template['subject'],values),render_template(template['body'],values),
            template['sender'],reply_to=template['reply_to'],sender_name=template.get('sender_name'))
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

def tick(db,*,sheets=None,engine_factory=Engine,mail_factory=GraphReports,renderer=None,max_jobs=20,
         response_id=None,response_reference=None,job_id=None,idempotency_key=None,stage=None):
    response_selector=bool(response_id or response_reference)
    job_selector=bool(job_id or idempotency_key)
    if response_id and response_reference:raise ValueError('Select a Forms response by ID or invitation reference, not both')
    if response_selector and not job_selector and (stage is not None or max_jobs!=0):
        raise ValueError('Exact-response ingestion-only tick requires max_jobs=0 and no job stage')
    if job_selector and (not job_id or not idempotency_key or stage not in {'RESEARCH','REPORT'} or max_jobs!=1):
        raise ValueError('Controlled job tick requires matching job ID/key, RESEARCH or REPORT stage, and max_jobs=1')
    if response_selector and job_selector:
        raise ValueError('Ingest the selected response separately before the exact-job invocation')
    if stage=='REPORT' and not job_id:raise ValueError('Report-only tick requires an exact job ID')
    if response_selector and job_selector:raise ValueError('A tick cannot ingest and execute at once')
    run_id=uuid.uuid4().hex;errors=[];processed=0
    subsystem_errors={'consent':[],'research':[],'report':[],'delivery':[]}
    with db.transaction() as c:c.execute(worker_runs.insert().values(id=run_id,started_at=now(),status='RUNNING'))
    try:
        if response_id or response_reference:
            rows=(sheets or Sheets()).read(response_id=response_id,response_reference=response_reference)
        elif not job_selector:
            rows=(sheets or Sheets()).read()
        else:
            rows=[]
        if getattr(rows,'identity_status','VERIFIED')!='VERIFIED':
            warning='Sheets identity verification: '+str(getattr(rows,'identity_error',None) or 'Google Forms verified respondent identity is unavailable')
            errors.append(warning);subsystem_errors['consent'].append(warning)
        for row in rows:
            if response_reference and parse_sheet_row(row.answers if isinstance(row,VerifiedFormResponse) else row).invitation_reference!=response_reference:
                continue
            if response_id and (not isinstance(row,VerifiedFormResponse) or row.identity.get('response_id')!=response_id):
                continue
            if isinstance(row,VerifiedFormResponse):process(db,row.answers,verified_identity=row.identity)
            else:process(db,row)
    except Exception as e:
        warning='Sheets: '+safe_error(e);errors.append(warning);subsystem_errors['consent'].append(warning)
    for _ in range(max_jobs):
        job=claim(db,job_id=job_id if job_selector else None,idempotency_key=idempotency_key if job_selector else None,
                  stage=stage,scheduled=not job_selector,controlled=job_selector)
        if not job:break
        try:
            stage=job['stage']
            if stage=='RESEARCH':research_step(db,job,engine_factory)
            elif stage=='REPORT':report_step(db,job,renderer)
            elif stage=='DELIVERY':delivery_step(db,job,mail_factory)
            else:raise ValueError('Unknown durable job stage')
            processed+=1
        except Exception as e:
            status='BLOCKED_CONSENT' if isinstance(e,PermissionError) else 'BLOCKED_CONFIGURATION' if isinstance(e,ConfigurationBlocked) else 'FAILED' if isinstance(e,ValueError) else 'RETRY_PENDING'
            error=safe_error(e);message=job['id']+': '+error;errors.append(message)
            subsystem_errors[{'RESEARCH':'research','REPORT':'report','DELIVERY':'delivery'}.get(job['stage'],'research')].append(message)
            with db.transaction(serialized=True) as c:
                row=c.execute(select(jobs.c.lease_token).where(jobs.c.id==job['id'])).first()
                if row and row.lease_token==job['lease_token']:
                    transition(c,job['id'],status=status,error=error,next_attempt_at=later(min(3600,60*2**min(job['attempts'],6))))
        finally:
            with db.transaction() as c:c.execute(jobs.update().where(jobs.c.id==job['id'],jobs.c.lease_token==job['lease_token']).values(lease_until=None,lease_token=None))
    personalization_backfill=None;personalization_error=None
    if response_selector or job_selector:
        personalization_backfill={'status':'DISABLED','disabled':True,'attempted':0,
            'reason':'Exact-response/job invocations cannot claim unrelated personalization work.'}
    else:
      try:
        # Existing scheduled worker doubles as a bounded, pre-consent V1
        # personalization checkpoint. This path never contacts the GEO Engine.
        from .personalization import backfill
        env=__import__('os').environ
        # Paid work is never performed by the recurring full worker. A pilot
        # must use the dedicated personalization-only command and an exact ID
        # allowlist; an empty allowlist always fails closed in backfill().
        if env.get('OUTREACH_AI_PERSONALIZATION_SCHEDULED_ENABLED','false').strip().casefold() in {'1','true','yes','on'}:
            personalization_backfill=backfill(db,
                limit=min(5,int(env.get('OUTREACH_AI_PERSONALIZATION_MAX_CANDIDATES_PER_RUN','5'))),
                concurrency=1,time_budget_seconds=25,
                authorized_candidate_ids=[x.strip() for x in env.get('OUTREACH_AI_PERSONALIZATION_COHORT_CANDIDATE_IDS','').split(',') if x.strip()],
                selected_candidate_ids=[x.strip() for x in env.get('OUTREACH_AI_PERSONALIZATION_REPROCESS_CANDIDATE_IDS','').split(',') if x.strip()])
        else:
            personalization_backfill={'status':'DISABLED','disabled':True,'attempted':0,
                'reason':'Personalization is isolated from the recurring full worker.'}
        processed+=personalization_backfill['attempted']
      except Exception:
        personalization_error='Bounded candidate batch failed; claimed candidates remain recoverable'
        errors.append('Personalization backfill: '+personalization_error)
    consent_status='ATTENTION_REQUIRED' if subsystem_errors['consent'] else 'COMPLETED'
    personalization_status=('ATTENTION_REQUIRED' if personalization_error else
        (personalization_backfill or {}).get('status','COMPLETED'))
    result={'worker_run_id':run_id,'invocation_status':'COMPLETED' if not errors else 'ATTENTION_REQUIRED',
            'status':'COMPLETED' if not errors else 'ATTENTION_REQUIRED','processed_stages':processed,'errors':errors,
            'subsystems':{
                'consent':{'status':consent_status,'errors':subsystem_errors['consent']},
                'personalization':{'status':personalization_status,'batch':personalization_backfill,'error':personalization_error},
                'research':{'status':'ATTENTION_REQUIRED' if subsystem_errors['research'] else 'COMPLETED','errors':subsystem_errors['research']},
                'report':{'status':'ATTENTION_REQUIRED' if subsystem_errors['report'] else 'COMPLETED','errors':subsystem_errors['report']},
                'delivery':{'status':'ATTENTION_REQUIRED' if subsystem_errors['delivery'] else 'COMPLETED','errors':subsystem_errors['delivery']},
            },
            'personalization_backfill':personalization_backfill}
    with db.transaction() as c:c.execute(worker_runs.update().where(worker_runs.c.id==run_id).values(finished_at=now(),status=result['status'],detail=dump(result)))
    return result

def safe_error(exc):
    # Only locally authored validation errors are safe to persist. HTTP and DB exceptions can contain secrets.
    if isinstance(exc,(ConfigurationBlocked,OutcomeUncertain,PermissionError,ValueError)):
        return str(exc)[:500]
    return type(exc).__name__+'; operation failed and remains recoverable'
