from __future__ import annotations
import hmac,json,os,uuid,hashlib,secrets,re
from datetime import datetime,timedelta,timezone
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select,func
from starlette.concurrency import run_in_threadpool
from .db import (Database,batches,invitations,jobs,reports,deliveries,worker_runs,events,now,
    admin_users,admin_invites,email_templates,questionnaires,pilot_cohorts,pilot_members,report_reviews,candidates as candidate_table)
from . import auth,service
from .migrate import dump
from outreach_agent.google_forms_integration import FORM_VIEW_URL,INVITATION_REFERENCE_ENTRY

STATIC=Path(__file__).parent/'static'
COOKIE='outreach_v2_session';CSRF='outreach_v2_csrf'
EMAIL_RE=re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

def create_app(database=None):
    app=FastAPI(title='GEO Research Outreach',docs_url=None,redoc_url=None,openapi_url=None)
    db=database or Database();app.state.db=db
    secure=os.environ.get('OUTREACH_COOKIE_SECURE','true').lower()!='false'
    @app.middleware('http')
    async def protect(request,call_next):
        path=request.url.path
        try:
            if path.startswith('/api/') and path not in {'/api/auth/login','/api/auth/session','/api/internal/worker-tick','/api/admin/invites/accept'}:
                current=auth.session(db,request.cookies.get(COOKIE))
                if not current:return JSONResponse({'error':'Your administrator session has expired. Please sign in.'},401)
                request.state.session=current
                if request.method not in {'GET','HEAD'} and not auth.csrf_valid(current,request.headers.get('x-csrf-token')):
                    return JSONResponse({'error':'Invalid CSRF token; refresh and try again.'},403)
            if request.method not in {'GET','HEAD','OPTIONS'}:
                origin=request.headers.get('origin')
                expected=os.environ.get('OUTREACH_PUBLIC_ORIGIN') or str(request.base_url).rstrip('/')
                if origin and origin!=expected:return JSONResponse({'error':'Origin is not allowed'},403)
            response=await call_next(request)
        except Exception:
            # Do not leak SQL parameters, credentials or company data in failures.
            response=JSONResponse({'error':'Service unavailable. Check the configured database and server logs.'},503)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response
    @app.exception_handler(ValueError)
    async def validation_error(request,exc):return JSONResponse({'error':str(exc)},422)
    @app.exception_handler(LookupError)
    async def missing(request,exc):return JSONResponse({'error':str(exc)},404)
    @app.exception_handler(PermissionError)
    async def denied(request,exc):return JSONResponse({'error':str(exc)},403)
    @app.get('/health')
    def health():
        with db.transaction() as c:c.execute(select(1))
        return {'status':'healthy','service':'outreach-v2','commit':os.environ.get('RENDER_GIT_COMMIT','local')}
    @app.get('/')
    def index():return FileResponse(STATIC/'index.html')
    app.mount('/static',StaticFiles(directory=STATIC),name='static')
    @app.post('/api/auth/login')
    async def login(request:Request):
        body=await request.json()
        if len(str(body.get('password','')))>1024:raise ValueError('Invalid credentials')
        result=auth.login(db,str(body.get('email','')),str(body.get('password','')),request.client.host)
        response=JSONResponse({'authenticated':True,'email':result['email'],'role':result['role'],'csrf':result['csrf']})
        response.set_cookie(COOKIE,result['token'],max_age=43200,httponly=True,secure=secure,samesite='strict',path='/')
        response.set_cookie(CSRF,result['csrf'],max_age=43200,httponly=True,secure=secure,samesite='strict',path='/')
        return response
    @app.get('/api/auth/session')
    def current(request:Request):
        row=auth.session(db,request.cookies.get(COOKIE));csrf=request.cookies.get(CSRF)
        if not row or not auth.csrf_valid(row,csrf):return JSONResponse({'authenticated':False},401)
        return {'authenticated':True,'email':row['email'],'role':row.get('role'),'csrf':csrf}
    @app.post('/api/auth/logout')
    def logout(request:Request):
        auth.logout(db,request.cookies.get(COOKIE,''));response=JSONResponse({'authenticated':False})
        for cookie in [COOKIE,CSRF]:response.delete_cookie(cookie,path='/',secure=secure,httponly=True,samesite='strict')
        return response
    @app.get('/api/dashboard')
    def dashboard():return service.dashboard(db)
    @app.get('/api/candidates')
    def candidates(request:Request):
        params=dict(request.query_params);page=int(params.pop('page',1));size=int(params.pop('page_size',50))
        return service.list_candidates(db,params,page,size)
    @app.get('/api/demo/lagoon')
    def lagoon_demo():
        from .demo import lagoon_case
        return lagoon_case()
    @app.get('/api/demo/lagoon/package')
    def lagoon_demo_package():
        from .demo import build_preview_package
        return Response(build_preview_package(),media_type='application/zip',
            headers={'Content-Disposition':'attachment; filename="lagoon-review-preview-not-sent.zip"'})
    @app.get('/api/candidates/{ident:path}')
    def candidate(ident:str):return service.detail(db,ident)
    @app.post('/api/batches')
    async def generate(request:Request):
        body=await request.json();result=service.generate(db,body,request.headers.get('idempotency-key'))
        return Response(result['package'],media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{result["id"]}.zip"','X-Batch-Id':result['id'],'X-Excluded-Count':str(len(json.loads(result['excluded'])))})
    @app.get('/api/batches')
    def batch_list(page:int=1):
        with db.transaction() as c:
            return {'rows':[dict(r) for r in c.execute(select(*[x for x in batches.c if x.name!='package']).order_by(batches.c.created_at.desc()).limit(50).offset((max(page,1)-1)*50)).mappings()]}
    @app.get('/api/batches/{ident}/download')
    def download(ident:str):
        with db.transaction() as c:row=c.execute(select(batches).where(batches.c.id==ident)).mappings().first()
        if not row:raise LookupError('Batch not found')
        return Response(row['package'],media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{ident}.zip"'})
    @app.post('/api/batches/{ident}/send-record')
    async def record_send(ident:str,request:Request):
        body=await request.json();evidence=str(body.get('evidence') or '').strip()
        if not evidence:raise ValueError('Describe the actual Outlook batch send evidence')
        # Operator attestation is not a provider receipt or delivery confirmation.
        with db.transaction(serialized=True) as c:
            if not c.scalar(select(batches.c.id).where(batches.c.id==ident)):raise LookupError('Batch not found')
            c.execute(batches.update().where(batches.c.id==ident).values(send_status='OPERATOR_REPORTED',send_evidence=evidence,send_recorded_at=now()))
            c.execute(invitations.update().where(invitations.c.batch_id==ident).values(send_status='OPERATOR_REPORTED'))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='OPERATOR_REPORTED_SEND',detail=evidence,created_at=now()))
        return {'status':'OPERATOR_REPORTED','verified_delivery':False}
    @app.get('/api/invitations')
    def invitation_list(page:int=1):
        with db.transaction() as c:return {'rows':[dict(r) for r in c.execute(select(invitations).order_by(invitations.c.generated_at.desc()).limit(50).offset((max(page,1)-1)*50)).mappings()]}
    @app.get('/api/research')
    def research(page:int=1):
        with db.transaction() as c:
            query=select(*[x for x in jobs.c if x.name!='result'],invitations.c.company_name,invitations.c.recipient,
                reports.c.id.label('report_id'),reports.c.sha256.label('report_sha256'),deliveries.c.status.label('delivery_status'),
                deliveries.c.provider_message_id).join(invitations).outerjoin(reports).outerjoin(deliveries)
            rows=[dict(r) for r in c.execute(query.order_by(jobs.c.created_at.desc()).limit(50).offset((max(page,1)-1)*50)).mappings()]
            for row in rows:
                latest=c.execute(select(report_reviews.c.decision,report_reviews.c.reviewer_email,report_reviews.c.reviewed_at)
                    .where(report_reviews.c.report_id==row['report_id'],report_reviews.c.document_sha256==row['report_sha256'])
                    .order_by(report_reviews.c.reviewed_at.desc()).limit(1)).mappings().first() if row['report_id'] else None
                row['review_status']=latest['decision'] if latest else ('PENDING_REVIEW' if row['report_id'] else None)
                row['reviewer_email']=latest['reviewer_email'] if latest else None
            return {'rows':rows}
    @app.get('/api/reports/{ident}')
    def report(ident:str):
        with db.transaction() as c:r=c.execute(select(reports).where(reports.c.id==ident)).mappings().first()
        if not r:raise LookupError('Report not found')
        return Response(r['document'],media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',headers={'Content-Disposition':f'attachment; filename="{ident}.docx"','X-Report-SHA256':r['sha256'] or ''})

    @app.get('/api/reports/{ident}/review')
    def report_review(ident:str):
        with db.transaction() as c:
            report=c.execute(select(reports.c.id,reports.c.sha256).where(reports.c.id==ident)).mappings().first()
            if not report:raise LookupError('Report not found')
            rows=c.execute(select(report_reviews).where(report_reviews.c.report_id==ident,
                report_reviews.c.document_sha256==report['sha256']).order_by(report_reviews.c.reviewed_at.desc())).mappings().all()
            latest=dict(rows[0]) if rows else None
            return {'report_id':ident,'document_sha256':report['sha256'],
                'status':latest['decision'] if latest else 'PENDING_REVIEW','review':latest}

    @app.post('/api/reports/{ident}/review')
    async def review_report(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','APPROVER')
        body=await request.json();decision=body.get('decision');expected=str(body.get('document_sha256') or '')
        if decision not in {'APPROVED','CHANGES_REQUESTED'}:raise ValueError('Decision must be APPROVED or CHANGES_REQUESTED')
        with db.transaction(serialized=True) as c:
            report=c.execute(select(reports).where(reports.c.id==ident)).mappings().first()
            if not report:raise LookupError('Report not found')
            actual=hashlib.sha256(report['document'] or b'').hexdigest()
            if not expected or not hmac.compare_digest(expected,actual) or not hmac.compare_digest(str(report['sha256'] or ''),actual):
                raise ValueError('The reviewed DOCX version changed; reload the report before reviewing')
            review_id='review-'+uuid.uuid4().hex
            c.execute(report_reviews.insert().values(id=review_id,report_id=ident,document_sha256=actual,decision=decision,
                reviewer_email=request.state.session['email'],reviewed_at=now(),note=str(body.get('note') or '')[:1000] or None))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='REPORT_'+decision,
                detail=json.dumps({'sha256':actual,'reviewer':request.state.session['email'],'review_id':review_id}),created_at=now()))
        return {'report_id':ident,'document_sha256':actual,'status':decision,'review_id':review_id}

    @app.get('/api/admin/users')
    def admin_list(request:Request):
        auth.require_role(request.state.session,'ADMIN')
        with db.transaction() as c:
            return {'rows':[dict(r) for r in c.execute(select(admin_users.c.id,admin_users.c.email,admin_users.c.role,
                admin_users.c.status,admin_users.c.created_at).order_by(admin_users.c.email)).mappings()]}

    @app.post('/api/admin/invites')
    async def create_admin_invite(request:Request):
        auth.require_role(request.state.session,'ADMIN')
        body=await request.json();email=str(body.get('email') or '').strip().lower();role=body.get('role')
        if not EMAIL_RE.fullmatch(email):raise ValueError('Enter a valid administrator email')
        if role not in {'ADMIN','APPROVER','OPERATOR'}:raise ValueError('Unsupported administrator role')
        if email==os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu').lower().strip():
            raise ValueError('The configured bootstrap administrator cannot be re-invited')
        token=secrets.token_urlsafe(32);invite_id='admin-invite-'+uuid.uuid4().hex
        with db.transaction(serialized=True) as c:
            if c.scalar(select(admin_users.c.id).where(admin_users.c.email==email)):raise ValueError('Administrator already exists')
            c.execute(admin_invites.insert().values(id=invite_id,email=email,role=role,token_hash=auth.digest(token),
                expires_at=(datetime.now(timezone.utc)+timedelta(hours=48)).isoformat(),
                invited_by=request.state.session['email'],accepted_at=None))
        # The bearer token is returned once for the operator to deliver over an
        # approved channel; this API never emails it or exposes it again.
        return {'invite_id':invite_id,'email':email,'role':role,'token':token,'expires_in_hours':48,
            'delivery':'MANUAL_HANDOFF_REQUIRED'}

    @app.post('/api/admin/invites/accept')
    async def accept_admin_invite(request:Request):
        body=await request.json();token=str(body.get('token') or '');password=str(body.get('password') or '')
        if len(password)<14 or len(password)>1024:raise ValueError('Password must contain 14 to 1024 characters')
        with db.transaction(serialized=True) as c:
            invite=c.execute(select(admin_invites).where(admin_invites.c.token_hash==auth.digest(token),
                admin_invites.c.accepted_at.is_(None),admin_invites.c.expires_at>now())).mappings().first()
            if not invite:raise PermissionError('Administrator invite is invalid or expired')
            if c.scalar(select(admin_users.c.id).where(admin_users.c.email==invite['email'])):raise ValueError('Administrator already exists')
            from argon2 import PasswordHasher
            c.execute(admin_users.insert().values(id='admin-'+uuid.uuid4().hex,email=invite['email'],
                password_hash=PasswordHasher().hash(password),role=invite['role'],status='ACTIVE',
                created_by=invite['invited_by'],created_at=now()))
            c.execute(admin_invites.update().where(admin_invites.c.id==invite['id']).values(accepted_at=now()))
        return {'created':True,'email':invite['email'],'role':invite['role']}

    @app.get('/api/admin/email-templates')
    def list_email_templates(request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        with db.transaction() as c:return {'rows':[dict(r) for r in c.execute(select(email_templates).order_by(email_templates.c.version.desc())).mappings()]}

    @app.post('/api/admin/email-templates')
    async def save_email_template(request:Request):
        auth.require_role(request.state.session,'ADMIN')
        body=await request.json();subject=str(body.get('subject') or '').strip();content=str(body.get('body') or '').strip()
        sender=str(body.get('sender') or '').strip()
        if not subject or len(subject)>200 or not content or len(content)>20000:raise ValueError('Template subject/body is empty or exceeds limits')
        configured_sender=os.environ.get('OUTREACH_REPORT_SENDER','').strip()
        if sender and configured_sender and sender.lower()!=configured_sender.lower():
            raise ValueError('Template sender must match the authorized report mailbox')
        with db.transaction(serialized=True) as c:
            version=(c.scalar(select(func.max(email_templates.c.version))) or 0)+1
            c.execute(email_templates.update().values(active=False));ident='email-template-'+uuid.uuid4().hex
            c.execute(email_templates.insert().values(id=ident,version=version,subject=subject,body=content,sender=sender or None,
                active=True,created_by=request.state.session['email'],created_at=now()))
        return {'id':ident,'version':version,'active':True,'sender_configured':bool(configured_sender and (not sender or sender.lower()==configured_sender.lower()))}

    @app.get('/api/admin/questionnaires')
    def list_questionnaires(request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR','APPROVER')
        with db.transaction() as c:return {'rows':[dict(r) for r in c.execute(select(questionnaires).order_by(questionnaires.c.version.desc())).mappings()]}

    @app.post('/api/admin/questionnaires')
    async def save_questionnaire(request:Request):
        auth.require_role(request.state.session,'ADMIN')
        body=await request.json();url=str(body.get('form_url') or '').strip();mapping=body.get('field_map')
        if not url.startswith('https://docs.google.com/forms/') or not isinstance(mapping,dict):
            raise ValueError('Use an existing Google Form URL and a field mapping object')
        existing_form=os.environ.get('GEO_RESEARCH_FORM_URL') or FORM_VIEW_URL
        existing_entry=os.environ.get('GEO_RESEARCH_FORM_ENTRY_ID') or INVITATION_REFERENCE_ENTRY
        if url.rstrip('/')!=existing_form.rstrip('/'):
            raise ValueError('Only the currently configured existing Google Form can be versioned')
        if set(mapping)-{'invitation_reference_entry_id'} or mapping.get('invitation_reference_entry_id',existing_entry)!=existing_entry:
            raise ValueError('Questionnaire mapping must preserve the existing invitation reference field')
        with db.transaction(serialized=True) as c:
            version=(c.scalar(select(func.max(questionnaires.c.version))) or 0)+1
            c.execute(questionnaires.update().values(active=False));ident='questionnaire-'+uuid.uuid4().hex
            c.execute(questionnaires.insert().values(id=ident,version=version,form_url=url,field_map=json.dumps(mapping,sort_keys=True),
                active=True,created_by=request.state.session['email'],created_at=now()))
        return {'id':ident,'version':version,'active':True}

    @app.post('/api/admin/pilot-cohorts')
    async def create_pilot_cohort(request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        body=await request.json();name=str(body.get('name') or '100-company pilot').strip()
        if len(name)>120:raise ValueError('Cohort name is too long')
        from .service import readiness
        with db.transaction(serialized=True) as c:
            rows=c.execute(select(candidate_table).where(candidate_table.c.kind=='company',readiness()).order_by(candidate_table.c.id).limit(100)).mappings().all()
            if len(rows)!=100:raise ValueError(f'Pilot cohort needs 100 eligible, personalized companies; currently {len(rows)} are available')
            cohort_id='pilot-'+uuid.uuid4().hex
            c.execute(pilot_cohorts.insert().values(id=cohort_id,name=name,target_size=100,
                created_by=request.state.session['email'],created_at=now()))
            c.execute(pilot_members.insert(),[{'cohort_id':cohort_id,'candidate_id':r['id'],'position':i+1,
                'eligibility_snapshot':json.dumps({'website_status':r['website_status'],'qualification':r['qualification'],
                    'personalization_status':r['personalization_status'],'website':r['website']},sort_keys=True)} for i,r in enumerate(rows)])
        return {'id':cohort_id,'name':name,'target_size':100,'member_count':100,'invitations_created':0,'send_status':'NOT_SENT'}

    @app.get('/api/admin/pilot-cohorts')
    def list_pilot_cohorts(request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR','APPROVER')
        with db.transaction() as c:return {'rows':[dict(r) for r in c.execute(select(pilot_cohorts).order_by(pilot_cohorts.c.created_at.desc())).mappings()]}

    @app.get('/api/admin/pilot-cohorts/{ident}')
    def pilot_cohort_detail(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR','APPROVER')
        with db.transaction() as c:
            cohort=c.execute(select(pilot_cohorts).where(pilot_cohorts.c.id==ident)).mappings().first()
            if not cohort:raise LookupError('Pilot cohort not found')
            members=c.execute(select(pilot_members.c.position,pilot_members.c.eligibility_snapshot,candidate_table.c.id,
                candidate_table.c.name,candidate_table.c.website).select_from(pilot_members).join(candidate_table,
                candidate_table.c.id==pilot_members.c.candidate_id).where(pilot_members.c.cohort_id==ident)
                .order_by(pilot_members.c.position)).mappings().all()
            return {'cohort':dict(cohort),'members':[dict(r) for r in members]}

    @app.post('/api/admin/pilot-cohorts/{ident}/package')
    async def generate_pilot_package(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        with db.transaction() as c:
            cohort=c.execute(select(pilot_cohorts.c.id).where(pilot_cohorts.c.id==ident)).first()
            if not cohort:raise LookupError('Pilot cohort not found')
            ids=[r[0] for r in c.execute(select(pilot_members.c.candidate_id).where(pilot_members.c.cohort_id==ident)
                .order_by(pilot_members.c.position)).all()]
        if len(ids)!=100:raise ValueError('Pilot cohort no longer contains exactly 100 saved members')
        batch=service.generate(db,{'ids':ids},'pilot-cohort-package-'+ident)
        return Response(batch['package'],media_type='application/zip',headers={
            'Content-Disposition':f'attachment; filename="{batch["id"]}.zip"','X-Batch-Id':batch['id'],
            'X-Excluded-Count':str(len(json.loads(batch['excluded'])))})
    @app.get('/api/settings')
    def settings():
        with db.transaction() as c:run=c.execute(select(worker_runs).order_by(worker_runs.c.started_at.desc()).limit(1)).mappings().first()
        return {'administrator':os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu'),
            'sheets_configured':bool(os.environ.get('GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')),
            'engine_url':os.environ.get('GEO_ENGINE_API_URL'),
            'engine_configured':bool(os.environ.get('GEO_ENGINE_HANDOFF_TOKEN')),
            'report_sender':os.environ.get('OUTREACH_REPORT_SENDER'),
            'report_delivery_configured':all(os.environ.get(k) for k in ['OUTREACH_REPORT_SENDER','OUTREACH_GRAPH_TENANT_ID','OUTREACH_GRAPH_CLIENT_ID','OUTREACH_GRAPH_CLIENT_SECRET']),
            'last_worker_run':dict(run) if run else None}
    @app.post('/api/internal/worker-tick')
    async def scheduled_worker_tick(request:Request):
        expected=os.environ.get('OUTREACH_WORKER_TOKEN','')
        supplied=request.headers.get('authorization','')
        if len(expected)<32 or not hmac.compare_digest(supplied,'Bearer '+expected):
            raise HTTPException(status_code=401,detail='Worker authentication required')
        if not db.postgres:
            raise HTTPException(status_code=503,detail='Scheduled worker requires PostgreSQL durable locking')
        try:
            if int(request.headers.get('content-length','0'))>1024:
                raise HTTPException(status_code=413,detail='Worker request is too large')
            raw=bytearray()
            async for chunk in request.stream():
                if len(raw)+len(chunk)>1024:
                    raise HTTPException(status_code=413,detail='Worker request is too large')
                raw.extend(chunk)
            body=json.loads(raw or b'{}')
        except json.JSONDecodeError:
            raise HTTPException(status_code=400,detail='Invalid worker request')
        if not isinstance(body,dict) or set(body)-{'max_jobs'}:
            raise HTTPException(status_code=400,detail='Invalid worker request')
        max_jobs=body.get('max_jobs',10)
        if type(max_jobs) is not int:
            raise HTTPException(status_code=400,detail='Invalid worker limit')
        if not 1<=max_jobs<=10:
            raise HTTPException(status_code=400,detail='Worker limit must be between 1 and 10')
        from .worker import tick
        with db.worker_lock() as acquired:
            if not acquired:
                raise HTTPException(status_code=409,detail='A worker invocation is already running')
            # The durable tick performs bounded network and database I/O. Keep
            # it off the ASGI event loop so health checks and other requests can
            # continue while a batch is running.
            result=await run_in_threadpool(tick,db,max_jobs=max_jobs)
        return result
    return app
