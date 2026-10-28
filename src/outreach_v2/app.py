from __future__ import annotations
import hmac,json,os,uuid
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from .db import Database,batches,invitations,jobs,reports,deliveries,worker_runs,events,now
from . import auth,service
from .migrate import dump

STATIC=Path(__file__).parent/'static'
COOKIE='outreach_v2_session';CSRF='outreach_v2_csrf'

def create_app(database=None):
    app=FastAPI(title='GEO Research Outreach',docs_url=None,redoc_url=None,openapi_url=None)
    db=database or Database();app.state.db=db
    secure=os.environ.get('OUTREACH_COOKIE_SECURE','true').lower()!='false'
    @app.middleware('http')
    async def protect(request,call_next):
        path=request.url.path
        try:
            if path.startswith('/api/') and path not in {'/api/auth/login','/api/auth/session','/api/internal/worker-tick'}:
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
        response=JSONResponse({'authenticated':True,'email':result['email'],'csrf':result['csrf']})
        response.set_cookie(COOKIE,result['token'],max_age=43200,httponly=True,secure=secure,samesite='strict',path='/')
        response.set_cookie(CSRF,result['csrf'],max_age=43200,httponly=True,secure=secure,samesite='strict',path='/')
        return response
    @app.get('/api/auth/session')
    def current(request:Request):
        row=auth.session(db,request.cookies.get(COOKIE));csrf=request.cookies.get(CSRF)
        if not row or not auth.csrf_valid(row,csrf):return JSONResponse({'authenticated':False},401)
        return {'authenticated':True,'email':row['email'],'csrf':csrf}
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
                reports.c.id.label('report_id'),deliveries.c.status.label('delivery_status'),deliveries.c.provider_message_id).join(invitations).outerjoin(reports).outerjoin(deliveries)
            return {'rows':[dict(r) for r in c.execute(query.order_by(jobs.c.created_at.desc()).limit(50).offset((max(page,1)-1)*50)).mappings()]}
    @app.get('/api/reports/{ident}')
    def report(ident:str):
        with db.transaction() as c:r=c.execute(select(reports).where(reports.c.id==ident)).mappings().first()
        if not r:raise LookupError('Report not found')
        return Response(r['document'],media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',headers={'Content-Disposition':f'attachment; filename="{ident}.docx"'})
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
