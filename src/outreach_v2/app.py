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
    admin_users,admin_invites,email_templates,questionnaires,pilot_cohorts,pilot_members,report_reviews,invitation_corrections,candidates as candidate_table)
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
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        body=await request.json();result=service.generate(db,body,request.headers.get('idempotency-key'))
        excluded=json.loads(result.get('excluded') or '[]')
        import io,zipfile
        with zipfile.ZipFile(io.BytesIO(result['package'])) as package:
            manifest=json.loads(package.read('manifest.json'))
        status=result.get('pilot_review_status','PENDING_REVIEW')
        return {'batch_id':result['id'],'review_status':status,
            'invitation_count':manifest['count'],'excluded_count':len(excluded),
            'download_available':status=='APPROVED','sent':False}
    @app.get('/api/batches')
    def batch_list(page:int=1):
        with db.transaction() as c:
            return {'rows':[dict(r) for r in c.execute(select(*[x for x in batches.c if x.name!='package']).order_by(batches.c.created_at.desc()).limit(50).offset((max(page,1)-1)*50)).mappings()]}
    @app.get('/api/batches/{ident}/download')
    def download(ident:str):
        with db.transaction() as c:row=c.execute(select(batches).where(batches.c.id==ident)).mappings().first()
        if not row:raise LookupError('Batch not found')
        if row['pilot_review_status']!='APPROVED':
            raise PermissionError('Invitation package is not available until the exact batch receives human approval')
        if row['pilot_review_status']=='APPROVED' and hashlib.sha256(row['package'] or b'').hexdigest()!=row['pilot_review_sha256']:
            raise PermissionError('Approved invitation package hash no longer matches; approval is invalidated')
        return Response(row['package'],media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{ident}.zip"'})
    @app.get('/api/batches/{ident}/review')
    def review_batch(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','APPROVER','OPERATOR')
        with db.transaction() as c:
            batch=c.execute(select(batches).where(batches.c.id==ident)).mappings().first()
            if not batch:raise LookupError('Batch not found')
            rows=c.execute(select(invitations,candidate_table.c.personalization_quality_status,
                candidate_table.c.personalization_quality_errors).select_from(invitations.join(candidate_table,
                candidate_table.c.id==invitations.c.candidate_id)).where(invitations.c.batch_id==ident)
                .order_by(invitations.c.company_name)).mappings().all()
            items=[]
            for row in rows:
                item=dict(row);item['personalization']=json.loads(item.get('personalization') or '{}')
                item['personalization_quality_errors']=json.loads(item.get('personalization_quality_errors') or '[]')
                items.append(item)
            return {'batch_id':ident,'status':batch['pilot_review_status'],'package_sha256':hashlib.sha256(batch['package'] or b'').hexdigest(),
                'excluded':json.loads(batch['excluded'] or '[]'),'invitations':items,'download_available':batch['pilot_review_status']=='APPROVED'}
    @app.post('/api/batches/{ident}/approve')
    async def approve_batch(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','APPROVER')
        body=await request.json();provided=str(body.get('package_sha256') or '')
        if body.get('confirm_reviewed') is not True:raise ValueError('Reviewer must confirm the complete invitation batch was reviewed')
        with db.transaction(serialized=True) as c:
            batch=c.execute(select(batches).where(batches.c.id==ident)).mappings().first()
            if not batch:raise LookupError('Batch not found')
            if batch['pilot_review_status'] not in {'PENDING_REVIEW','BLOCKED_INCOMPLETE','NOT_REQUIRED'}:raise ValueError('Batch is not awaiting review')
            digest=hashlib.sha256(batch['package'] or b'').hexdigest()
            if not provided or not hmac.compare_digest(provided,digest):raise ValueError('Approval must match the current complete package SHA-256')
            rows=c.execute(select(invitations.c.id,invitations.c.website,invitations.c.personalization,
                candidate_table.c.personalization_quality_status).select_from(invitations.join(candidate_table,
                candidate_table.c.id==invitations.c.candidate_id)).where(invitations.c.batch_id==ident)).all()
            if not rows:raise ValueError('Empty invitation batch cannot be approved')
            for row in rows:
                if row.personalization_quality_status!='PASS':raise ValueError('Every invitation must pass the current personalization quality gate')
                service.validate(json.loads(row.personalization or '{}'),row.website)
            stamp=now();actor=request.state.session['email']
            c.execute(batches.update().where(batches.c.id==ident).values(pilot_review_status='APPROVED',pilot_review_sha256=digest,
                pilot_reviewed_by=actor,pilot_reviewed_at=stamp,send_status='READY_TO_SEND'))
            c.execute(invitations.update().where(invitations.c.batch_id==ident).values(send_status='READY_TO_SEND'))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='INVITATION_BATCH_APPROVED',
                detail=json.dumps({'sha256':digest,'count':len(rows),'reviewer':actor},sort_keys=True),created_at=stamp))
        return {'batch_id':ident,'status':'APPROVED','package_sha256':digest,'send_status':'READY_TO_SEND','sent':False}
    @app.post('/api/batches/{ident}/send-record')
    async def record_send(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        body=await request.json();evidence=str(body.get('evidence') or '').strip()
        if not evidence:raise ValueError('Describe the actual Outlook batch send evidence')
        # Operator attestation is not a provider receipt or delivery confirmation.
        with db.transaction(serialized=True) as c:
            batch=c.execute(select(batches).where(batches.c.id==ident)).mappings().first()
            if not batch:raise LookupError('Batch not found')
            if batch['pilot_review_status']!='APPROVED':
                raise PermissionError('Invitation batch must be reviewed before recording a send')
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
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=invite_id,event='ADMIN_INVITE_CREATED',
                detail=json.dumps({'email':email,'role':role,'invited_by':request.state.session['email']},sort_keys=True),created_at=now()))
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
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=invite['id'],event='ADMIN_INVITE_ACCEPTED',
                detail=json.dumps({'email':invite['email'],'role':invite['role']},sort_keys=True),created_at=now()))
        return {'created':True,'email':invite['email'],'role':invite['role']}

    @app.get('/api/admin/email-templates')
    def list_email_templates(request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR','APPROVER')
        with db.transaction() as c:return {'rows':[dict(r) for r in c.execute(select(email_templates).order_by(email_templates.c.version.desc())).mappings()]}

    @app.post('/api/admin/email-templates')
    async def save_email_template(request:Request):
        auth.require_role(request.state.session,'ADMIN')
        body=await request.json();subject=str(body.get('subject') or '').strip();content=str(body.get('body') or '').strip()
        sender=str(body.get('sender') or '').strip()
        kind=str(body.get('kind') or 'REPORT').strip().upper()
        if kind not in {'REPORT','INVITATION'}:raise ValueError('Template kind must be REPORT or INVITATION')
        reply_to=str(body.get('reply_to') or '').strip()
        researcher_name=str(body.get('researcher_name') or '').strip()
        affiliation=str(body.get('university_affiliation') or '').strip()
        if kind=='INVITATION':
            from .template import validate_invitation_template
            validate_invitation_template(subject,content,sender=sender,reply_to=reply_to,
                researcher_name=researcher_name,university_affiliation=affiliation)
            configured_sender=os.environ.get('OUTREACH_INVITATION_SENDER','').strip()
            if configured_sender and sender.lower()!=configured_sender.lower():
                raise ValueError('Invitation sender must match the configured researcher mailbox')
        else:
            if not subject or len(subject)>200 or not content or len(content)>20000:raise ValueError('Template subject/body is empty or exceeds limits')
            configured_sender=os.environ.get('OUTREACH_REPORT_SENDER','').strip()
            if sender and configured_sender and sender.lower()!=configured_sender.lower():
                raise ValueError('Template sender must match the authorized report mailbox')
        with db.transaction(serialized=True) as c:
            version=(c.scalar(select(func.max(email_templates.c.version))) or 0)+1
            c.execute(email_templates.update().where(email_templates.c.template_kind==kind).values(active=False));ident='email-template-'+uuid.uuid4().hex
            c.execute(email_templates.insert().values(id=ident,version=version,subject=subject,body=content,sender=sender or None,
                active=True,created_by=request.state.session['email'],created_at=now(),template_kind=kind,
                reply_to=reply_to or None,researcher_name=researcher_name or None,university_affiliation=affiliation or None))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='EMAIL_TEMPLATE_VERSION_CREATED',
                detail=json.dumps({'kind':kind,'version':version,'created_by':request.state.session['email']},sort_keys=True),created_at=now()))
        return {'id':ident,'version':version,'kind':kind,'active':True,
            'sender_configured':bool(configured_sender and (not sender or sender.lower()==configured_sender.lower()))}

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
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='QUESTIONNAIRE_MAPPING_VERSION_CREATED',
                detail=json.dumps({'version':version,'created_by':request.state.session['email']},sort_keys=True),created_at=now()))
        return {'id':ident,'version':version,'active':True}

    @app.post('/api/admin/pilot-cohorts')
    async def create_pilot_cohort(request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        body=await request.json();name=str(body.get('name') or '100-company pilot').strip()
        if len(name)>120:raise ValueError('Cohort name is too long')
        from .service import select_pilot_cohort
        with db.transaction(serialized=True) as c:
            rows=select_pilot_cohort(c,100)
            if len(rows)!=100:raise ValueError(f'Pilot cohort needs 100 eligible, personalized companies; currently {len(rows)} are available')
            cohort_id='pilot-'+uuid.uuid4().hex
            c.execute(pilot_cohorts.insert().values(id=cohort_id,name=name,target_size=100,
                created_by=request.state.session['email'],created_at=now()))
            c.execute(pilot_members.insert(),[{'cohort_id':cohort_id,'candidate_id':r['id'],'position':i+1,
                'eligibility_snapshot':json.dumps({'website_status':r['website_status'],'qualification':r['qualification'],
                    'personalization_status':r['personalization_status'],'personalization_quality_status':r['personalization_quality_status'],
                    'website':r['website'],'industry':r['industry']},sort_keys=True)} for i,r in enumerate(rows)])
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
        excluded=json.loads(batch['excluded'])
        with db.transaction() as c:
            c.execute(batches.update().where(batches.c.id==batch['id']).values(
                pilot_review_status='PENDING_REVIEW' if len(excluded)==0 else 'BLOCKED_INCOMPLETE'))
        manifest=json.loads(__import__('zipfile').ZipFile(__import__('io').BytesIO(batch['package'])).read('manifest.json'))
        return {'batch_id':batch['id'],'review_status':'PENDING_REVIEW' if not excluded else 'BLOCKED_INCOMPLETE',
            'invitation_count':manifest['count'],'excluded_count':len(excluded),'download_available':False,'sent':False}

    @app.get('/api/admin/pilot-cohorts/{ident}/review')
    def pilot_cohort_review(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','APPROVER','OPERATOR')
        request_key='pilot-cohort-package-'+ident
        with db.transaction() as c:
            batch=c.execute(select(batches).where(batches.c.request_key==request_key)).mappings().first()
            if not batch: raise LookupError('Pilot package has not been generated')
            rows=c.execute(select(invitations,candidate_table.c.personalization_quality_status,
                candidate_table.c.personalization_quality_errors,
                invitations.c.personalization.label('invitation_personalization')).select_from(
                invitations.join(candidate_table,candidate_table.c.id==invitations.c.candidate_id))
                .where(invitations.c.batch_id==batch['id']).order_by(invitations.c.company_name)).mappings().all()
            invitations_review=[]
            for row in rows:
                item=dict(row);item['evidence']=json.loads(item.pop('invitation_personalization') or '{}')
                item['quality_errors']=json.loads(item.pop('personalization_quality_errors') or '[]')
                invitations_review.append(item)
            return {'batch_id':batch['id'],'status':batch['pilot_review_status'],'send_status':batch['send_status'],
                'package_sha256':hashlib.sha256(batch['package'] or b'').hexdigest(),
                'excluded':json.loads(batch['excluded'] or '[]'),'expected_recipients':100,
                'actual_recipients':len(invitations_review),'invitations':invitations_review}

    @app.post('/api/admin/pilot-cohorts/{ident}/approve')
    async def approve_pilot_batch(ident:str,request:Request):
        auth.require_role(request.state.session,'APPROVER','ADMIN')
        body=await request.json();provided=str(body.get('package_sha256') or '')
        if body.get('confirm_reviewed') is not True: raise ValueError('Reviewer must explicitly confirm the complete batch was reviewed')
        request_key='pilot-cohort-package-'+ident
        stamp=now()
        with db.transaction(serialized=True) as c:
            batch=c.execute(select(batches).where(batches.c.request_key==request_key)).mappings().first()
            if not batch: raise LookupError('Pilot package has not been generated')
            rows=c.execute(select(invitations.c.id,invitations.c.candidate_id,invitations.c.personalization,
                invitations.c.website,candidate_table.c.personalization_quality_status).select_from(
                invitations.join(candidate_table,candidate_table.c.id==invitations.c.candidate_id)).where(invitations.c.batch_id==batch['id'])).all()
            digest=hashlib.sha256(batch['package'] or b'').hexdigest()
            if provided!=digest: raise ValueError('Approval must match the current complete package SHA-256')
            quality_ok=True
            for row in rows:
                try: service.validate(json.loads(row.personalization or '{}'),row.website)
                except (ValueError,TypeError): quality_ok=False
            if len(rows)!=100 or json.loads(batch['excluded'] or '[]') or any(r.personalization_quality_status!='PASS' for r in rows) or not quality_ok:
                raise ValueError('Pilot batch must contain exactly 100 quality-passed recipients with no exclusions')
            c.execute(batches.update().where(batches.c.id==batch['id']).values(pilot_review_status='APPROVED',
                pilot_review_sha256=digest,pilot_reviewed_by=request.state.session['email'],pilot_reviewed_at=stamp,
                send_status='READY_TO_SEND'))
            c.execute(invitations.update().where(invitations.c.batch_id==batch['id']).values(send_status='READY_TO_SEND'))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=batch['id'],event='PILOT_BATCH_APPROVED',
                detail=json.dumps({'sha256':digest,'count':100,'reviewer':request.state.session['email']},sort_keys=True),created_at=stamp))
        return {'batch_id':batch['id'],'status':'APPROVED','send_status':'READY_TO_SEND','sent':False,'package_sha256':digest}

    @app.post('/api/admin/batches/{ident}/mark-not-sent')
    async def mark_batch_not_sent(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        body=await request.json()
        if body.get('attest_not_sent') is not True: raise ValueError('Explicit operator not-sent attestation is required')
        with db.transaction(serialized=True) as c:
            batch=c.execute(select(batches).where(batches.c.id==ident)).mappings().first()
            if not batch: raise LookupError('Batch not found')
            if batch['send_status'] in {'SENT','PARTIALLY_SENT'}: raise ValueError('Sent batch cannot be marked NOT_SENT')
            c.execute(batches.update().where(batches.c.id==ident).values(send_status='NOT_SENT',send_evidence='Operator attested not sent',send_recorded_at=now()))
            c.execute(invitations.update().where(invitations.c.batch_id==ident).values(send_status='NOT_SENT'))
            c.execute(events.insert().values(id=uuid.uuid4().hex,entity_id=ident,event='BATCH_MARKED_NOT_SENT',
                detail=json.dumps({'actor':request.state.session['email']},sort_keys=True),created_at=now()))
        return {'batch_id':ident,'send_status':'NOT_SENT'}

    @app.post('/api/admin/invitations/{ident}/corrections')
    async def correct_invitation(ident:str,request:Request):
        auth.require_role(request.state.session,'ADMIN','OPERATOR')
        body=await request.json()
        result=service.create_invitation_correction(db,ident,request.state.session['email'],
            attest_not_sent=body.get('attest_not_sent'))
        return result

    @app.post('/api/admin/invitation-corrections/{ident}/approve')
    async def approve_invitation_correction(ident:str,request:Request):
        auth.require_role(request.state.session,'APPROVER')
        body=await request.json()
        result=service.approve_invitation_correction(db,ident,request.state.session['email'],body.get('sha256'))
        return Response(result['package'],media_type='application/zip',headers={
            'Content-Disposition':f'attachment; filename="invitation-correction-v{result["version"]}.zip"',
            'X-Correction-Id':result['id'],'X-Correction-SHA256':result['sha256']})
    @app.get('/api/settings')
    def settings():
        with db.transaction() as c:run=c.execute(select(worker_runs).order_by(worker_runs.c.started_at.desc()).limit(1)).mappings().first()
        return {'administrator':os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu'),
            'sheets_configured':bool(os.environ.get('GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')),
            'engine_url':os.environ.get('GEO_ENGINE_API_URL'),
            'engine_configured':bool(os.environ.get('GEO_ENGINE_HANDOFF_TOKEN')),
            'report_sender':os.environ.get('OUTREACH_REPORT_SENDER'),
            'researcher_identity_configured':all(os.environ.get(k) for k in ['OUTREACH_RESEARCHER_NAME','OUTREACH_UNIVERSITY_AFFILIATION','OUTREACH_REPLY_TO']),
            'researcher_name':os.environ.get('OUTREACH_RESEARCHER_NAME'),
            'university_affiliation':os.environ.get('OUTREACH_UNIVERSITY_AFFILIATION'),
            'reply_to':os.environ.get('OUTREACH_REPLY_TO'),
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
