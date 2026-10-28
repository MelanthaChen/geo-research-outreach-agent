import csv,io,json,zipfile,sqlite3,threading,hashlib,shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import pytest
from sqlalchemy import select,func
from outreach_v2.db import Database,candidates,contacts,invitations,batches,jobs,submissions,deliveries,reports,report_reviews,sessions,worker_runs,now,sources,source_rows,source_memberships,pilot_members,email_templates
from outreach_v2.service import generate,list_candidates
from outreach_v2.consent import process
from outreach_v2.template import render,render_template,BULK_SUBJECT
from outreach_v2.migrate import Migrator,dump,pack,sha,postgres_text
from outreach_v2.worker import tick,claim,research_step,report_step,delivery_step
from outreach_v2.integrations import ConfigurationBlocked,OutcomeUncertain,Engine,verified_form_rows,VerifiedFormResponse,_sheet_row
from outreach_agent.google_forms_integration import parse_sheet_row

@pytest.fixture
def db(tmp_path):
    result=Database('sqlite:///'+str(tmp_path/'test.db'));result.initialize();return result

def candidate(db,ident='a',email='yuxuan.chen.1031@outlook.com',website='https://geo-ai-resume.vercel.app',name='GeoAIResume',suppressed=False,personalization=True,fixture=True):
    p={'data_classification':'OBSERVED','business_description':'Resume building website.','company_strength':'The page describes resume building features.',
       'source_urls':[website],'retrieved_at':now(),'recommendations':[{'title':'Page title','problem':'The sampled page has no title.','suggestion':'Add a descriptive page title.','source_urls':[website],'observed_at':now()}]}
    with db.transaction() as c:
        c.execute(candidates.insert().values(id=ident,kind='company',name=name,website=website,domain=website.split('//')[1],industry='Software',location='Unknown',website_status='VERIFIED',qualification='ELIGIBLE',geography='UNKNOWN',suppressed=suppressed,source='isolated-unit-test',personalization=dump(p if personalization else {}),personalization_status='READY' if personalization else 'INCOMPLETE',fixture=fixture,created_at=now(),updated_at=now()))
        c.execute(contacts.insert().values(id='contact-'+ident,candidate_id=ident,kind='EMAIL',email=email,source_url=website,verification='OWNER_AUTHORIZED' if fixture else 'VERIFIED',suppressed=False))
    return ident

def invite(db,ids=['a'],key='stable-request-key-123'):
    return generate(db,{'ids':ids},key)

def response(db,interest='YES',authorization='YES',id='a',email=None):
    with db.transaction() as c:r=c.execute(select(invitations).where(invitations.c.candidate_id==id)).mappings().one()
    return {'Timestamp':'10/27/2026 22:30:00','Invitation Reference':r['reference'],'Business Name':r['company_name'],'Business Website':r['website'],
      'Is your company interested in participating in this research?':interest,
      'Does your company authorize our research team to conduct the research described above?':authorization,
      'Full Name of the Authorized Company Representative':'Yuxuan Chen','Job Title of the Authorized Company Representative':'Owner',
      'Business Email Address of the Authorized Company Representative':email or r['recipient'],
      'Do you confirm that you are authorized to provide this permission on behalf of your business?':'Yes'}

def count(db,table):
    with db.transaction() as c:return c.scalar(select(func.count()).select_from(table))

def test_two_recipient_package_and_exact_binding(db):
    candidate(db);candidate(db,'b','yuxaun.chen.1031@outlook.com','https://whisker-haven-demo.vercel.app/','Whisker Haven')
    batch=invite(db,['a','b']);z=zipfile.ZipFile(io.BytesIO(batch['package']))
    rows=list(csv.DictReader(io.StringIO(z.read('invitations.csv').decode('utf-8-sig'))))
    assert len(rows)==2
    assert [(r['company_name'],r['recipient_email']) for r in rows]==[('GeoAIResume','yuxuan.chen.1031@outlook.com'),('Whisker Haven','yuxaun.chen.1031@outlook.com')]
    assert len({r['invitation_reference'] for r in rows})==2
    from openpyxl import load_workbook
    workbook=load_workbook(io.BytesIO(z.read('invitations.xlsx')))
    assert list(rows[0])==[c.value for c in workbook.active[1]]
    xml=zipfile.ZipFile(io.BytesIO(z.read('invitation-mail-merge.docx'))).read('word/document.xml').decode()
    import re
    fields=re.findall('MERGEFIELD (body_paragraph_\\d+)',xml)
    assert fields and set(fields)<=set(rows[0])
    from docx import Document
    preview=Document(io.BytesIO(z.read('personalized-previews.docx')))
    preview_text='\n'.join(p.text for p in preview.paragraphs)
    for r in rows:
        assert r['invitation_reference'] in r['email_body'] and r['questionnaire_url'] in r['email_body']
        assert r['bulk_subject']==BULK_SUBJECT
        assert r['Email Address']==r['recipient_email']
        assert r['company_name'] in preview_text and r['recipient_email'] in preview_text
        assert r['email_subject'] in preview_text and r['invitation_reference'] in preview_text
        assert r['questionnaire_url'] in preview_text and r['bulk_subject'] in preview_text
    with db.transaction() as c:
        saved=c.execute(select(invitations).order_by(invitations.c.candidate_id)).mappings().all()
        assert [json.loads(x['authority'])['method'] for x in saved]==['OWNER_BINDING','OWNER_BINDING']
        assert [x['website'] for x in saved]==['https://geo-ai-resume.vercel.app','https://whisker-haven-demo.vercel.app/']
    assert batch['send_status']=='UNVERIFIED'
    assert invite(db,['a','b'])['package']==batch['package']
    assert count(db,invitations)==2

def test_batch_excludes_bad_record_and_duplicate_recipient(db):
    candidate(db);candidate(db,'b',suppressed=True);candidate(db,'c',personalization=False);candidate(db,'d')
    result=invite(db,['a','b','c','d']);excluded=json.loads(result['excluded'])
    assert len(excluded)==3 and count(db,invitations)==1
    assert 'Duplicate recipient within batch' in excluded[-1]['reasons']

def test_select_all_filtered_not_page_limited(db):
    for i in range(3):candidate(db,str(i))
    data=list_candidates(db,{'q':'GeoAI'},1,1)
    assert len(data['rows'])==1 and data['eligible_matching']==3
    result=generate(db,{'all_matching':True,'filters':{'q':'GeoAI'}},'select-all-request-123')
    # all three were considered, two duplicate email addresses were excluded.
    assert len(json.loads(result['excluded']))==2

@pytest.mark.parametrize('interest,authorization',[('YES','NO'),('NO','YES'),('NO','NO'),('YES','')])
def test_negative_consent_never_starts(db,interest,authorization):
    candidate(db);invite(db);process(db,response(db,interest,authorization));assert count(db,jobs)==0

def test_unknown_reference_blocks_and_removed_scope_fields_are_optional(db):
    candidate(db);invite(db);row=response(db);row['Invitation Reference']='GEO-INVALID'
    assert process(db,row)=='BLOCKED_REFERENCE';assert count(db,jobs)==0
    row=response(db);row.pop('Business Name');row.pop('Business Website')
    assert process(db,row)=='AUTHORIZED';assert count(db,jobs)==1

def test_ordinary_scope_is_resolved_from_invitation_not_self_reported_fields(db):
    candidate(db);invite(db);row=response(db)
    row['Business Name']='An unrelated self-reported name';row['Business Website']='https://other.example'
    assert process(db,row)=='AUTHORIZED';assert count(db,jobs)==1

def test_historical_reference_does_not_authorize_clean_invitation(db):
    candidate(db);invite(db);row=response(db);row['Invitation Reference']='historical-response-reference'
    assert process(db,row)=='BLOCKED_REFERENCE';assert count(db,jobs)==0

def test_owner_consent_is_automatic_and_duplicate_safe(db):
    candidate(db);invite(db);row=response(db)
    assert process(db,row)=='AUTHORIZED';assert count(db,jobs)==1
    assert process(db,row)=='DUPLICATE';row['Timestamp']='10/27/2026 22:31:00'
    assert process(db,row)=='DUPLICATE_CONSENT';assert count(db,jobs)==1

def test_geoairesume_saved_blocked_scope_revalidates_once_and_ordinary_mismatch_stays_blocked(db):
    from outreach_v2.db import events
    candidate(db,ident='geoairesume-owner-test-v1',name='GeoAIResume',fixture=True)
    invite(db,['geoairesume-owner-test-v1'],key='geoairesume-recovery-test-key')
    ref='GEO-A67A912BDB53E20447BD1E323C935738'
    row=response(db,id='geoairesume-owner-test-v1');row['Invitation Reference']=ref
    row['Business Name']='GeoAIResume'
    with db.transaction() as c:
        inv=c.execute(select(invitations).where(invitations.c.candidate_id=='geoairesume-owner-test-v1')).mappings().one()
        c.execute(invitations.update().where(invitations.c.id==inv['id']).values(reference=ref,company_name='GeoAIResume controlled test website',consent_status='BLOCKED_SCOPE'))
        ident='submission-'+sha(dump({'answers':row,'verified_identity':{}}))
        c.execute(submissions.insert().values(id=ident,invitation_id=inv['id'],reference=ref,interest='YES',authorization='YES',
            representative_name='Yuxuan Chen',representative_role='Owner',representative_email=inv['recipient'],submitted_at=row['Timestamp'],
            received_at=now(),identity_provider=None,identity_verified_email=None,identity_reference=None,status='BLOCKED_SCOPE',
            reason='Declared business identity or website differs from the invitation',raw=dump({'answers':row,'verified_identity':{}})))
    assert process(db,row)=='AUTHORIZED'
    assert process(db,row)=='DUPLICATE'
    with db.transaction() as c:
        saved=c.execute(select(submissions).where(submissions.c.id==ident)).mappings().one()
        invitation=c.execute(select(invitations).where(invitations.c.reference==ref)).mappings().one()
        job=c.execute(select(jobs).where(jobs.c.invitation_id==invitation['id'])).mappings().one()
        history=c.execute(select(events).where(events.c.entity_id==invitation['id'],events.c.event=='AUTHORIZED')).mappings().one()
        assert saved['status']=='AUTHORIZED' and json.loads(saved['raw'])['answers']==row
        assert invitation['consent_status']=='AUTHORIZED'
        assert job['submission_id']==ident and count(db,jobs)==1
        assert 'Declared business identity' in history['detail']

    candidate(db,ident='ordinary-company',email='owner@ordinary.example',website='https://ordinary.example',name='Ordinary Company',fixture=False)
    invite(db,['ordinary-company'],key='ordinary-mismatch-test-key')
    ordinary=response(db,id='ordinary-company');ordinary['Business Name']='Ordinary Co';ordinary['Business Website']='https://unrelated.example'
    # Respondent-entered scope labels are ignored; this non-fixture still cannot
    # authorize from a self-reported business email without verified identity.
    assert process(db,ordinary)=='BLOCKED_IDENTITY'
    with db.transaction() as c:
        assert c.scalar(select(jobs.c.id).where(jobs.c.invitation_id==invitations.c.id).where(invitations.c.candidate_id=='ordinary-company')) is None

def test_withdrawn_candidate_cannot_create_research_job(db):
    candidate(db);invite(db)
    with db.transaction() as c:c.execute(candidates.update().values(suppressed=True))
    assert process(db,response(db))=='WITHDRAWN';assert count(db,jobs)==0

def test_foreign_representative_does_not_redirect_report(db):
    candidate(db);invite(db);row=response(db,email='someone@other.example')
    assert process(db,row)=='BLOCKED_IDENTITY';assert count(db,jobs)==0
    with db.transaction() as c:assert c.scalar(select(invitations.c.recipient))=='yuxuan.chen.1031@outlook.com'

def test_real_company_requires_google_verified_invited_mailbox(db):
    candidate(db,ident='real',email='research@company.example',website='https://company.example',name='Example Company',fixture=False)
    invite(db,['real'],key='real-invite-key-12345')
    row=response(db,id='real',email='research@company.example')
    assert process(db,row)=='BLOCKED_IDENTITY'
    assert count(db,jobs)==0
    identity={'provider':'GOOGLE_FORMS_VERIFIED','email':'research@company.example','response_id':'forms-response-1','email_collection_type':'VERIFIED'}
    assert process(db,row,verified_identity=identity)=='AUTHORIZED'
    assert count(db,jobs)==1
    with db.transaction() as c:
        sub=c.execute(select(submissions).where(submissions.c.identity_reference=='forms-response-1')).mappings().one()
        assert sub['identity_provider']=='GOOGLE_FORMS_VERIFIED' and sub['identity_verified_email']=='research@company.example'

@pytest.mark.parametrize('email',[None,'someone@company.example','research@other.example'])
def test_real_company_verified_identity_must_match_invited_recipient(db,email):
    candidate(db,ident='real',email='research@company.example',website='https://company.example',name='Example Company',fixture=False)
    invite(db,['real'],key='real-invite-key-12345')
    row=response(db,id='real',email='research@company.example')
    identity={'provider':'GOOGLE_FORMS_VERIFIED','email':email or '','response_id':'forms-response-1','email_collection_type':'VERIFIED'}
    assert process(db,row,verified_identity=identity)=='BLOCKED_IDENTITY'
    assert count(db,jobs)==0

def test_google_forms_verified_rows_require_verified_collection_and_match_sheet_contract():
    from outreach_v2.consent import REQUIRED_HEADERS
    form={'settings':{'emailCollectionType':'VERIFIED'},'items':[{'title':h,'questionItem':{'question':{'questionId':str(i)}}} for i,h in enumerate(sorted(REQUIRED_HEADERS-{'Timestamp'}))]}
    row={'responseId':'r1','respondentEmail':'research@company.example','lastSubmittedTime':'2026-10-28T06:00:00Z',
         'answers':{str(i):{'textAnswers':{'answers':[{'value':'GEO-1'}]}} for i,_ in enumerate(sorted(REQUIRED_HEADERS-{'Timestamp'}))}}
    historical=row|{'responseId':'old','lastSubmittedTime':'2026-10-27T22:15:44Z'}
    result=verified_form_rows(form,[historical,row],list(REQUIRED_HEADERS))
    assert len(result)==1 and isinstance(result[0],VerifiedFormResponse)
    assert result[0].identity['email']=='research@company.example'
    assert result[0].answers['Invitation Reference']=='GEO-1'
    form['settings']['emailCollectionType']='RESPONDER_INPUT'
    with pytest.raises(ConfigurationBlocked,match='VERIFIED'):verified_form_rows(form,[row],list(REQUIRED_HEADERS))

def test_sheet_aliases_and_duplicate_headers_are_canonical_and_fail_closed():
    row=_sheet_row(
        ['Your Role / Position','Job Title of the Authorized Company Representative',
         'Business Name','Business Name',
         'Do you confirm that you are authorized to provide this permission on behalf of your business?',
         'Do you confirm that you are authorized to provide this permission on behalf of your business?'],
        ['Founder','','GeoAIResume','','Yes',''])
    parsed=parse_sheet_row(row)
    assert parsed.representative_role=='Founder'
    assert row['Business Name']=='GeoAIResume'
    assert row['Do you confirm that you are authorized to provide this permission on behalf of your business?']=='Yes'
    conflict=_sheet_row(['Business Name','Business Name'],['GeoAIResume','Other Company'])
    assert conflict['Business Name']==''

def test_existing_sqlite_schema_gets_additive_identity_migration(tmp_path):
    path=tmp_path/'prior.db'
    with sqlite3.connect(path) as c:
        c.execute('create table submissions (id text primary key, invitation_id text, reference text, interest text, authorization text, representative_name text, representative_role text, representative_email text, submitted_at text, received_at text, status text, reason text, raw text)')
    migrated=Database('sqlite:///'+str(path));migrated.initialize();migrated.initialize()
    with migrated.engine.connect() as c:
        names={row[1] for row in c.exec_driver_sql('pragma table_info(submissions)')}
        assert {'identity_provider','identity_verified_email','identity_reference'}<=names
        assert c.exec_driver_sql('select count(*) from schema_migrations').scalar_one()==4

def test_conflicting_submission_blocks_queued_job(db):
    candidate(db);invite(db);process(db,response(db));row=response(db,'YES','NO');row['Timestamp']='10/27/2026 22:32:00';process(db,row)
    with db.transaction() as c:assert c.scalar(select(jobs.c.status))=='BLOCKED_CONSENT'

def test_concurrent_requests_create_one_batch(db):
    candidate(db)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:invite(db),range(2)))
    assert results[0]['id']==results[1]['id'];assert count(db,batches)==1
    with pytest.raises(ValueError,match='different selection'):invite(db,['other'])

def test_auth_sessions_csrf_restart_and_private_endpoints(db,monkeypatch):
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from outreach_v2.app import create_app
    monkeypatch.setenv('OUTREACH_ADMIN_PASSWORD_HASH',PasswordHasher().hash('test-password-only'))
    monkeypatch.setenv('OUTREACH_COOKIE_SECURE','false')
    client=TestClient(create_app(db));assert client.get('/api/candidates').status_code==401
    assert client.post('/api/auth/login',json={'email':'chen.yuxuan9@northeastern.edu','password':'bad'}).status_code==403
    login=client.post('/api/auth/login',json={'email':'chen.yuxuan9@northeastern.edu','password':'test-password-only'});assert login.status_code==200
    csrf=login.json()['csrf'];assert client.get('/api/candidates').status_code==200
    assert client.post('/api/auth/logout').status_code==403
    restarted=TestClient(create_app(Database(str(db.engine.url))));restarted.cookies.update(client.cookies)
    assert restarted.get('/api/auth/session').json()['authenticated'] is True
    assert restarted.post('/api/auth/logout',headers={'X-CSRF-Token':csrf}).status_code==200
    assert client.get('/api/candidates').status_code==401

def test_scheduled_worker_endpoint_auth_limits_and_postgres_lock(db,monkeypatch):
    from fastapi.testclient import TestClient
    from outreach_v2.app import create_app
    token='test-worker-token-'+('x'*32)
    monkeypatch.setenv('OUTREACH_WORKER_TOKEN',token)
    with db.worker_lock() as acquired:assert acquired is False
    offline=TestClient(create_app(db))
    assert offline.post('/api/internal/worker-tick',headers={'Authorization':'Bearer '+token},json={}).status_code==503
    db.postgres=True
    locked=True
    @contextmanager
    def lock():yield locked
    monkeypatch.setattr(db,'worker_lock',lock)
    observed=[]
    monkeypatch.setattr('outreach_v2.worker.tick',lambda database,max_jobs:observed.append((database,max_jobs)) or {'worker_run_id':'run-test','status':'COMPLETED','processed_stages':0,'errors':[]})
    client=TestClient(create_app(db))
    assert client.post('/api/internal/worker-tick',json={}).status_code==401
    headers={'Authorization':'Bearer '+token}
    assert client.post('/api/internal/worker-tick',headers=headers,content=b' '*1025).status_code==413
    assert client.post('/api/internal/worker-tick',headers=headers,json={'max_jobs':11}).status_code==400
    assert client.post('/api/internal/worker-tick',headers=headers,json={'max_jobs':'1'}).status_code==400
    assert client.post('/api/internal/worker-tick',headers=headers,json={'unexpected':True}).status_code==400
    response=client.post('/api/internal/worker-tick',headers=headers,json={'max_jobs':4})
    assert response.status_code==200 and response.json()['worker_run_id']=='run-test'
    assert observed==[(db,4)]
    locked=False
    assert client.post('/api/internal/worker-tick',headers=headers,json={}).status_code==409

class EmptySheets:
    def read(self):return []

def test_scheduler_configuration_failure_is_truthful(db):
    candidate(db);invite(db);process(db,response(db))
    def unavailable():raise ConfigurationBlocked('Engine unavailable for this isolated test')
    result=tick(db,sheets=EmptySheets(),engine_factory=unavailable)
    assert result['status']=='ATTENTION_REQUIRED'
    with db.transaction() as c:
        assert c.scalar(select(jobs.c.status))=='BLOCKED_CONFIGURATION'
        assert c.scalar(select(jobs.c.completed_at)) is None
    assert count(db,reports)==0 and count(db,deliveries)==0

def test_worker_consent_intake_automatically_creates_job_without_research_execution(db,monkeypatch):
    candidate(db);invite(db)
    submitted=response(db)
    class ConsentSheets:
        def read(self):return [VerifiedFormResponse(submitted,{'provider':'OWNER_BINDING','email':'yuxuan.chen.1031@outlook.com'})]
    class ReadOnlyEngine:
        def reconcile(self,key):return None
        def start(self,*_):pytest.fail('Research must not start while execution is disabled')
        def advance(self,*_):pytest.fail('Research must not advance while execution is disabled')
    monkeypatch.delenv('GEO_OUTREACH_RESEARCH_ENABLED',raising=False)
    result=tick(db,sheets=ConsentSheets(),engine_factory=ReadOnlyEngine)
    assert result['status']=='ATTENTION_REQUIRED'
    with db.transaction() as c:
        invitation=c.execute(select(invitations).where(invitations.c.candidate_id=='a')).mappings().one()
        sub=c.execute(select(submissions).where(submissions.c.reference==invitation['reference'])).mappings().one()
        job=c.execute(select(jobs).where(jobs.c.invitation_id==invitation['id'])).mappings().one()
        assert sub['status']=='AUTHORIZED'
        assert job['stage']=='RESEARCH' and job['status']=='BLOCKED_CONFIGURATION'
        run=c.execute(select(worker_runs).where(worker_runs.c.id==result['worker_run_id'])).mappings().one()
        assert run['finished_at'] is not None
    assert count(db,reports)==0 and count(db,deliveries)==0

def _authorized_leased_job(db):
    candidate(db);invite(db);assert process(db,response(db))=='AUTHORIZED'
    return claim(db)

def _completed_engine_result(job):
    return {'status':'COMPLETED','job_id':'engine-job-existing','property_id':'property-existing',
        'audit_id':'audit-existing','opportunity_ids':['opportunity-existing'],
        'result':{'record_id':'result-existing','contract_version':'geo-research-integration/v1',
            'source_profile_id':'a','source_profile_version':job['invitation_id'],'property_id':'property-existing',
            'site':'https://geo-ai-resume.vercel.app','retrieved_at':now(),
            'observed_pages':[{'url':'https://geo-ai-resume.vercel.app/','title':'Resume tools',
                'headings':['Build a resume'],'exact_quotes':['Create your resume online.']}],
            'recommendations':[{'rank':1,'title':'Clarify resume builder value','page_url':'https://geo-ai-resume.vercel.app/',
                'original_quote':'Create your resume online.','proposed_revision':'Build a polished resume online.',
                'rationale':'The page should describe the core outcome.'}],
            'questions':['What resume formats are supported?'],
            'baseline':{'provider':'exa','model':'gpt-6-luna','model_calls':1,'response_archive':'archive-existing',
                'results':[{'query':'resume builder'}],'interpretation':'Provider observations recorded.'},
            'limitations':['One bounded test result.']}}

def test_disabled_outreach_reconciles_completed_engine_result_and_creates_docx_without_delivery(db,monkeypatch):
    leased=_authorized_leased_job(db)
    monkeypatch.delenv('GEO_OUTREACH_RESEARCH_ENABLED',raising=False)
    engine_result=_completed_engine_result(leased)
    class ExistingCompletedEngine:
        def reconcile(self,key):
            assert key==leased['idempotency_key'];return engine_result
        def start(self,*_):pytest.fail('Disabled Outreach must not start research')
        def advance(self,*_):pytest.fail('Disabled Outreach must not advance research')
    research_step(db,leased,ExistingCompletedEngine)
    with db.transaction() as c:stored=dict(c.execute(select(jobs).where(jobs.c.id==leased['id'])).mappings().one())
    assert stored['stage']=='REPORT' and stored['status']=='COMPLETED'
    stored|={'lease_token':leased['lease_token']}
    report_step(db,stored)
    with db.transaction() as c:
        report=dict(c.execute(select(reports).where(reports.c.job_id==leased['id'])).mappings().one())
        final=dict(c.execute(select(jobs).where(jobs.c.id==leased['id'])).mappings().one())
    assert final['stage']=='DELIVERY' and final['status']=='COMPLETED'
    assert report['result_id']=='result-existing' and report['document'].startswith(b'PK')
    with zipfile.ZipFile(io.BytesIO(report['document'])) as docx:
        assert 'word/document.xml' in docx.namelist()
    assert count(db,deliveries)==0

def test_report_delivery_requires_approval_of_exact_docx_version(db,monkeypatch):
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from outreach_v2.app import create_app
    leased=_authorized_leased_job(db)
    class CompletedEngine:
        def reconcile(self,key):return _completed_engine_result(leased)
    research_step(db,leased,CompletedEngine)
    with db.transaction() as c:leased=dict(c.execute(select(jobs).where(jobs.c.id==leased['id'])).mappings().one())|{'lease_token':leased['lease_token']}
    report_step(db,leased,renderer=lambda _result:b'PK\x03\x04isolated-docx-v1')
    with db.transaction() as c:
        report=dict(c.execute(select(reports).where(reports.c.job_id==leased['id'])).mappings().one())
    class Mail:
        def __init__(self):self.calls=[]
        def configure_message(self,subject,body,sender):self.calls.append(('configure',subject,body,sender))
        def reconcile(self,key):self.calls.append(('reconcile',key));return None
        def create(self,key,recipient,document):self.calls.append(('create',recipient,document));return {'id':'draft-1'}
        def send(self,message_id):self.calls.append(('send',message_id));return {'status':'ACCEPTED'}
    mail=Mail()
    def forbidden_mail_factory():pytest.fail('Mail provider must not be initialized before exact-version review')
    with pytest.raises(ConfigurationBlocked,match='Exact DOCX version awaits approval'):
        delivery_step(db,leased,forbidden_mail_factory)
    assert count(db,deliveries)==0
    monkeypatch.setenv('OUTREACH_REPORT_SENDER','reports@example.org')
    with db.transaction() as c:
        c.execute(email_templates.insert().values(id='mail-template-test',version=1,subject='Approved report notice',
            body='Your attached report is ready.',sender='reports@example.org',active=True,
            created_by='chen.yuxuan9@northeastern.edu',created_at=now()))
    monkeypatch.setenv('OUTREACH_ADMIN_PASSWORD_HASH',PasswordHasher().hash('local-only-test-password'))
    monkeypatch.setenv('OUTREACH_COOKIE_SECURE','false')
    client=TestClient(create_app(db));login=client.post('/api/auth/login',json={
        'email':'chen.yuxuan9@northeastern.edu','password':'local-only-test-password'})
    assert login.status_code==200 and login.json()['role']=='ADMIN'
    csrf=login.json()['csrf'];headers={'X-CSRF-Token':csrf}
    stale=client.post(f"/api/reports/{report['id']}/review",headers=headers,
        json={'decision':'APPROVED','document_sha256':'0'*64})
    assert stale.status_code==422
    approved=client.post(f"/api/reports/{report['id']}/review",headers=headers,
        json={'decision':'APPROVED','document_sha256':report['sha256']})
    assert approved.status_code==200 and approved.json()['status']=='APPROVED'
    changes=client.post(f"/api/reports/{report['id']}/review",headers=headers,
        json={'decision':'CHANGES_REQUESTED','document_sha256':report['sha256'],'note':'Clarify one finding.'})
    assert changes.status_code==200
    with pytest.raises(ConfigurationBlocked,match='Exact DOCX version awaits approval'):
        delivery_step(db,leased,forbidden_mail_factory)
    assert count(db,deliveries)==0
    reapproved=client.post(f"/api/reports/{report['id']}/review",headers=headers,
        json={'decision':'APPROVED','document_sha256':report['sha256']})
    assert reapproved.status_code==200
    delivery_step(db,leased,lambda:mail)
    assert [call[0] for call in mail.calls]==['configure','reconcile','create','send']
    assert mail.calls[0]==('configure','Approved report notice','Your attached report is ready.','reports@example.org')
    assert mail.calls[2][2]==report['document']
    with db.transaction() as c:
        current=dict(c.execute(select(reports).where(reports.c.id==report['id'])).mappings().one())
        review=dict(c.execute(select(report_reviews).where(report_reviews.c.report_id==report['id'])
            .order_by(report_reviews.c.reviewed_at.desc()).limit(1)).mappings().one())
    assert review['document_sha256']==current['sha256']==hashlib.sha256(current['document']).hexdigest()
    assert count(db,deliveries)==1

def test_admin_invites_are_one_time_and_role_scoped(db,monkeypatch):
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from outreach_v2.app import create_app
    monkeypatch.setenv('OUTREACH_ADMIN_PASSWORD_HASH',PasswordHasher().hash('local-only-test-password'))
    monkeypatch.setenv('OUTREACH_COOKIE_SECURE','false')
    client=TestClient(create_app(db));login=client.post('/api/auth/login',json={
        'email':'chen.yuxuan9@northeastern.edu','password':'local-only-test-password'})
    csrf=login.json()['csrf'];headers={'X-CSRF-Token':csrf}
    invite_result=client.post('/api/admin/invites',headers=headers,json={'email':'reviewer@example.org','role':'APPROVER'})
    assert invite_result.status_code==200 and invite_result.json()['delivery']=='MANUAL_HANDOFF_REQUIRED'
    token=invite_result.json()['token']
    accepted=client.post('/api/admin/invites/accept',json={'token':token,'password':'local-only-strong-password'})
    assert accepted.status_code==200 and accepted.json()['role']=='APPROVER'
    assert client.post('/api/admin/invites/accept',json={'token':token,'password':'local-only-strong-password'}).status_code==403
    reviewer=TestClient(create_app(db));review_login=reviewer.post('/api/auth/login',json={
        'email':'reviewer@example.org','password':'local-only-strong-password'})
    assert review_login.status_code==200 and review_login.json()['role']=='APPROVER'
    assert reviewer.get('/api/admin/users').status_code==403

def test_admin_can_version_existing_questionnaire_and_email_preset_and_create_exact_100_person_pilot(db,monkeypatch):
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from outreach_v2.app import create_app
    for i in range(100):
        candidate(db,ident=f'pilot-{i:03}',email=f'contact{i}@pilot-example.org',
            website=f'https://company-{i:03}.pilot-example.org',name=f'Pilot Company {i:03}',fixture=False)
    monkeypatch.setenv('OUTREACH_ADMIN_PASSWORD_HASH',PasswordHasher().hash('local-only-test-password'))
    monkeypatch.setenv('OUTREACH_COOKIE_SECURE','false');monkeypatch.setenv('OUTREACH_REPORT_SENDER','reports@example.org')
    client=TestClient(create_app(db));login=client.post('/api/auth/login',json={
        'email':'chen.yuxuan9@northeastern.edu','password':'local-only-test-password'})
    headers={'X-CSRF-Token':login.json()['csrf']}
    form='https://docs.google.com/forms/d/e/existing-form-id/viewform'
    monkeypatch.setenv('GEO_RESEARCH_FORM_URL',form);monkeypatch.setenv('GEO_RESEARCH_FORM_ENTRY_ID','entry.123456')
    first=client.post('/api/admin/questionnaires',headers=headers,json={'form_url':form,
        'field_map':{'invitation_reference_entry_id':'entry.123456'}})
    second=client.post('/api/admin/questionnaires',headers=headers,json={'form_url':form,
        'field_map':{'invitation_reference_entry_id':'entry.123456'}})
    assert first.status_code==second.status_code==200
    question_rows=client.get('/api/admin/questionnaires').json()['rows']
    assert len(question_rows)==2 and [r['active'] for r in question_rows]==[True,False]
    saved=client.post('/api/admin/email-templates',headers=headers,json={'subject':'Research report',
        'body':'Your report is ready.','sender':'reports@example.org'})
    assert saved.status_code==200 and saved.json()['sender_configured'] is True
    cohort=client.post('/api/admin/pilot-cohorts',headers=headers,json={'name':'Cohort 1'})
    assert cohort.status_code==200 and cohort.json()['member_count']==100
    assert cohort.json()['invitations_created']==0 and count(db,invitations)==0
    cohort_id=cohort.json()['id']
    detail=client.get(f'/api/admin/pilot-cohorts/{cohort_id}')
    assert detail.status_code==200 and len(detail.json()['members'])==100
    first_package=client.post(f'/api/admin/pilot-cohorts/{cohort_id}/package',headers=headers,json={})
    second_package=client.post(f'/api/admin/pilot-cohorts/{cohort_id}/package',headers=headers,json={})
    assert first_package.status_code==second_package.status_code==200
    manifest=json.loads(zipfile.ZipFile(io.BytesIO(first_package.content)).read('manifest.json'))
    assert manifest['count']==100 and manifest['excluded']==[]
    assert first_package.headers['X-Batch-Id']==second_package.headers['X-Batch-Id']
    assert count(db,invitations)==100 and count(db,submissions)==count(db,jobs)==0
    candidate(db,ident='questionnaire-check',email='check@external-example.org',website='https://check.external-example.org',
        name='Questionnaire Check',fixture=False)
    package=invite(db,['questionnaire-check'],key='questionnaire-versioned-package-123')
    with db.transaction() as c:
        saved=c.execute(select(invitations).where(invitations.c.candidate_id=='questionnaire-check')).mappings().one()
    assert saved['questionnaire_id']==second.json()['id']
    assert 'entry.123456' in saved['form_url'] and form.split('/viewform')[0] in saved['form_url']
    with db.transaction() as c:
        assert c.scalar(select(func.count()).select_from(pilot_members).where(pilot_members.c.cohort_id==cohort_id))==100

def test_disabled_outreach_does_not_start_or_advance_incomplete_engine_job(db,monkeypatch):
    leased=_authorized_leased_job(db)
    monkeypatch.delenv('GEO_OUTREACH_RESEARCH_ENABLED',raising=False)
    class ExistingQueuedEngine:
        def reconcile(self,key):return {'status':'QUEUED','job_id':'engine-job-existing','property_id':'property-existing'}
        def start(self,*_):pytest.fail('Disabled Outreach must not start research')
        def advance(self,*_):pytest.fail('Disabled Outreach must not advance research')
    research_step(db,leased,ExistingQueuedEngine)
    with db.transaction() as c:
        stored=dict(c.execute(select(jobs).where(jobs.c.id==leased['id'])).mappings().one())
    assert stored['stage']=='RESEARCH' and stored['status']=='QUEUED'
    assert stored['engine_job_id']=='engine-job-existing' and stored['result'] is None

def test_migration_preserves_hotels_and_resume(db,tmp_path):
    source=tmp_path/'hotel.db'
    with sqlite3.connect(source) as c:
        c.execute('create table candidates(candidate_id text primary key,company_name text,website text,location text)')
        c.executemany('insert into candidates values(?,?,?,?)',[('h1','Hotel One','https://chain.example','Boston'),('h2','Hotel Two','https://chain.example','Chicago')])
        c.execute('create table hotel_contact_channels(candidate_id text,email text,source_url text)')
        c.execute("insert into hotel_contact_channels values('h2','chicago@chain.example','https://chain.example/chicago')")
    m=Migrator(db,batch_size=1);m.sqlite(source,'hotel');before=m.report();Migrator(db,batch_size=1).sqlite(source,'hotel')
    assert count(db,candidates)==2 and count(db,contacts)==1
    assert before['counts_reconcile']
    with db.transaction() as c:assert c.scalar(select(contacts.c.candidate_id))=='h2'
    with sqlite3.connect(source) as c:assert c.execute('select count(*) from candidates').fetchone()[0]==2

def test_operational_destination_cannot_be_imported(db):
    with pytest.raises(ValueError,match='operational database'):Migrator(db).sqlite(Path(db.engine.url.database))

def test_lean_snapshot_import_keeps_archive_refs_without_duplicate_payloads(tmp_path):
    source_path=tmp_path/'reconciled.db';archive_path=tmp_path/'protected-archive.db';manifest_path=tmp_path/'archive-manifest.json'
    source=Database('sqlite:///'+str(source_path));source.initialize()
    raw='{"name":"Example","website":"https://example.test"}';checksum=sha(raw)
    p={'data_classification':'OBSERVED','business_description':'Example business description.',
       'company_strength':'The homepage describes its service.','source_urls':['https://example.test'],
       'retrieved_at':now(),'recommendations':[{'title':'Page title','problem':'No page title.','suggestion':'Add a clear title.',
       'source_urls':['https://example.test'],'observed_at':now()}]}
    with source.transaction() as c:
        c.execute(candidates.insert().values(id='company-1',kind='company',name='Example',website='https://example.test',
            domain='example.test',industry='Software',location='Boston',description=p['business_description'],website_status='VERIFIED',
            qualification='ELIGIBLE',geography='UNKNOWN',suppressed=False,source='source.db',personalization=dump(p),
            personalization_status='READY',fixture=False,created_at=now(),updated_at=now()))
        c.execute(contacts.insert().values(id='contact-1',candidate_id='company-1',kind='EMAIL',email='hello@example.test',
            source_url='https://example.test/contact',evidence='Published contact page',verification='VERIFIED',
            purpose='business',suppressed=False,raw=raw))
        c.execute(sources.insert().values(id='source-1',path='source.db',relation='candidates',fingerprint='f',expected=1,
            processed=1,status='COMPLETE',updated_at=now()))
        c.execute(source_rows.insert().values(id='archive-row-1',source_id='source-1',source_key='company-1',candidate_id='company-1',
            disposition='PRESERVED',reason='',checksum=checksum,raw=pack(raw)))
        c.execute(source_memberships.insert().values(id='membership-1',source_id='source-1',offset=0,count=1,payload=b'archived'))
    with source.engine.connect() as c:c.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)')
    source.engine.dispose();shutil.copy2(source_path,archive_path);archive_path.chmod(0o600)
    digest=hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps({'source_snapshot':str(archive_path),'bytes':archive_path.stat().st_size,
        'sha256':digest,'sqlite_integrity_check':'ok'}))
    target=Database('sqlite:///'+str(tmp_path/'target.db'));target.initialize()
    result=Migrator(target).lean_operational_snapshot(archive_path,archive_manifest=manifest_path,
        expected={'candidates':{'company':1},'contacts':1,'isolated_rows':0,'source_memberships':1})
    with target.transaction() as c:
        imported=c.execute(select(candidates).where(candidates.c.id=='company-1')).mappings().one()
        contact=c.execute(select(contacts).where(contacts.c.id=='contact-1')).mappings().one()
        assert c.scalar(select(func.count()).select_from(source_rows))==0
        assert c.scalar(select(func.count()).select_from(source_memberships))==0
        assert c.scalar(select(func.count()).select_from(sources))==1
    assert imported['source_ref']=='source_row:archive-row-1' and imported['source_checksum']==checksum
    assert contact['source_ref']=='source_row:archive-row-1' and contact['raw'] is None
    assert result['candidates']=={'company':1} and result['contacts']==1
    assert result['source_archive_rows_preserved_outside_v2']==1 and result['v2_source_rows']==0

def test_v1_personalization_backfill_is_bounded_and_resumable(db):
    from outreach_v2.personalization import backfill, observe
    candidate(db,ident='real-backfill',email='hello@example.com',website='https://example.com',name='Example',
        personalization=False,fixture=False)
    page='<html><head><title>Example services</title><meta name="description" content="Example provides software services to small businesses."></head><body><h1>Example services</h1><p>Example provides software services to small businesses and explains its core product offering.</p></body></html>'
    fetch=lambda url:('https://example.com/',page)
    with pytest.raises(ValueError,match='between 1 and 100'):backfill(db,limit=101,fetcher=fetch)
    result=backfill(db,limit=1,fetcher=fetch)
    with db.transaction() as c:
        row=c.execute(select(candidates).where(candidates.c.id=='real-backfill')).mappings().one()
    assert result['attempted']==1 and result['completed']==1 and result['incomplete']==0
    assert result['remaining_backlog']==0
    assert result['failure_categories']=={} and result['permanent_insufficiency']==0
    assert row['personalization_status']=='READY' and row['personalization']!='{}'
    assert json.loads(row['personalization'])['recommendations'][0]['title']=='Answer common customer questions'
    assert not any(x['title'] in {'Page title','Page description'} for x in json.loads(row['personalization'])['recommendations'])
    partial=observe('https://example.com/',page,now(),truncated=True)
    assert partial['evidence']['content_sample_truncated'] is True
    assert backfill(db,limit=1,fetcher=fetch)['attempted']==0

def test_personalization_expands_to_three_grounded_same_site_pages():
    from outreach_v2.personalization import _evidence_urls
    html='''<a href="/about">About</a><a href="/services">Services</a>
        <a href="/products">Products</a><a href="/faq">FAQ</a>
        <a href="https://external.example/about">About</a>'''
    urls=_evidence_urls('https://example.test/',html)
    assert len(urls)==3
    assert all(url.startswith('https://example.test/') for url in urls)
    assert not any('external.example' in url for url in urls)

def test_personalization_backfill_claims_distinct_domains_and_persists_transient_backoff(db):
    from outreach_v2.personalization import backfill
    from outreach_v2.db import candidates
    from sqlalchemy import select
    import httpx
    candidate(db,ident='batch-a',email='hello@batch-a.example',website='https://batch-a.example',name='Batch A',personalization=False,fixture=False)
    candidate(db,ident='batch-b',email='hello@batch-b.example',website='https://batch-b.example',name='Batch B',personalization=False,fixture=False)
    candidate(db,ident='batch-c',email='hello@batch-a.example',website='https://www.batch-a.example',name='Batch A duplicate domain',personalization=False,fixture=False)
    page='<html><head><title>Company</title><meta name="description" content="A company that provides a factual service to its customers."></head><body><h1>Company</h1><p>This company provides useful services to customers in the local community.</p></body></html>'
    def transient(url):
        if 'batch-b' in url:raise httpx.ReadTimeout('temporary timeout')
        return url,page
    result=backfill(db,limit=3,fetcher=transient,concurrency=2)
    assert result['attempted']==2
    assert result['completed']==1 and result['incomplete']==1
    assert result['failure_categories']=={'TRANSIENT_HTTP':1}
    assert result['remaining_backlog']==2
    with db.transaction() as c:
        rows={r.id:dict(r) for r in c.execute(select(candidates).where(candidates.c.id.in_(['batch-a','batch-b','batch-c']))).mappings()}
    assert rows['batch-a']['personalization_status']=='READY'
    assert rows['batch-b']['personalization_outcome']=='TRANSIENT_HTTP'
    assert rows['batch-b']['personalization_attempts']==1
    assert rows['batch-b']['personalization_retry_after']
    assert rows['batch-c']['personalization_lease_token'] is None
    # A second worker invocation in the same scheduler window must honor the
    # durable 15-minute domain cooldown as well as candidate retry backoff.
    assert backfill(db,limit=3,fetcher=transient,concurrency=2)['attempted']==0

def test_robots_server_error_is_classified_as_transient(monkeypatch):
    import httpx
    import outreach_v2.personalization as personalization
    request=httpx.Request('GET','https://example.test/robots.txt')
    response=httpx.Response(503,request=request)
    class Client:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url):return response
    monkeypatch.setattr(personalization,'public_url',lambda url:None)
    monkeypatch.setattr(personalization.httpx,'Client',Client)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        personalization.fetch_page('https://example.test/')
    assert personalization._failure_kind(raised.value)=='TRANSIENT_HTTP'

def test_postgres_text_keeps_embedded_nul_visible():
    assert postgres_text('before\x00after')=='before\ufffdafter'
    assert postgres_text(None) is None

def test_engine_reconciliation_reports_sanitized_http_auth_stage():
    class Response:
        status_code=401
    class Client:
        def get(self,path):
            assert path.startswith('/api/v1/integration/outreach-jobs/')
            return Response()
    engine=Engine.__new__(Engine);engine.client=Client()
    with pytest.raises(ConfigurationBlocked,match=r'job reconciliation GET authorization denied: HTTP 401'):
        engine.reconcile('existing-stable-key')

def test_geoairesume_candidate_paths_with_existing_invitation_and_job(db,monkeypatch):
    """Regression for the live fixture shape that previously broke candidate search."""
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from outreach_v2.app import create_app

    geo_id='geoairesume-owner-test-v1'
    candidate(db,ident=geo_id,name='GeoAIResume controlled test website')
    candidate(db,ident='ordinary-company',email='contact@ordinary.example',
              website='https://ordinary.example',name='Ordinary Company')
    invite(db,[geo_id],key='geoairesume-existing-invitation-123')
    assert process(db,response(db,id=geo_id))=='AUTHORIZED'

    # Exercise the same service call and record relationships that exist in Neon.
    found=list_candidates(db,{'q':'GeoAIResume'})
    assert found['total']==1 and found['rows'][0]['id']==geo_id
    assert found['rows'][0]['invitation']['consent_status']=='AUTHORIZED'
    assert found['rows'][0]['research']['status']
    assert found['rows'][0]['delivery'] is None
    ordinary=list_candidates(db,{})
    assert {row['id'] for row in ordinary['rows']}=={geo_id,'ordinary-company'}
    assert list_candidates(db,{'q':'Ordinary Company'})['total']==1

    monkeypatch.setenv('OUTREACH_ADMIN_PASSWORD_HASH',PasswordHasher().hash('local-only-test-password'))
    monkeypatch.setenv('OUTREACH_COOKIE_SECURE','false')
    client=TestClient(create_app(db))
    login=client.post('/api/auth/login',json={
        'email':'chen.yuxuan9@northeastern.edu','password':'local-only-test-password'})
    assert login.status_code==200
    assert client.get('/api/candidates').status_code==200
    search=client.get('/api/candidates',params={'q':'GeoAIResume'})
    assert search.status_code==200 and search.json()['rows'][0]['id']==geo_id
    detail_response=client.get('/api/candidates/'+geo_id)
    assert detail_response.status_code==200 and detail_response.json()['id']==geo_id
    overview=client.get('/api/dashboard')
    assert overview.status_code==200 and overview.json()['companies']==2
    research=client.get('/api/research')
    assert research.status_code==200
    assert any(row['invitation_id'] for row in research.json()['rows'])

def test_invitation_content_preserves_legitimate_braces_and_flags_only_template_placeholders():
    from copy import deepcopy
    from outreach_v2.template import render
    p={'data_classification':'OBSERVED','business_description':'A page with JSON-like content.',
       'company_strength':'The site publishes {"availability": "open"} as an example.',
       'source_urls':['https://geo-ai-resume.vercel.app'],'retrieved_at':now(),
       'introduction':'The observed snippet includes {one or more options}.',
       'recommendations':[{'title':'Observed {schema} example','problem':'The page shows {"mode": "public"}.',
         'suggestion':'Keep the {field_name} clear.','source_urls':['https://geo-ai-resume.vercel.app'],'observed_at':now()}]}
    original=deepcopy(p)
    _,body=render('GeoAIResume','https://geo-ai-resume.vercel.app',p,'https://forms.example/form','GEO-EXAMPLE',True)
    assert '{"availability": "open"}' in body
    assert '{one or more options}' in body
    assert '{"mode": "public"}' in body and '{field_name}' in body
    assert p==original
    with pytest.raises(ValueError,match=r'Unresolved invitation variable: \{\{company_name\}\}'):
        render_template('Invitation for {{company_name}}',{})

def test_mixed_invitation_batch_excludes_invalid_personalization_and_is_idempotent(db):
    candidate(db,'valid','yuxuan.chen.1031@outlook.com')
    candidate(db,'invalid','invalid@geo-ai-resume.vercel.app')
    with db.transaction() as c:
        c.execute(candidates.update().where(candidates.c.id=='invalid').values(
            personalization=dump({'data_classification':'OBSERVED','company_strength':'observed',
                'source_urls':['https://geo-ai-resume.vercel.app'],'retrieved_at':now(),
                'recommendations':[{'problem':'observed','suggestion':'suggested',
                    'source_urls':['https://geo-ai-resume.vercel.app'],'observed_at':now()}]})))
    key='mixed-valid-invalid-invitation-request'
    batch=generate(db,{'ids':['valid','invalid']},key)
    manifest=json.loads(zipfile.ZipFile(io.BytesIO(batch['package'])).read('manifest.json'))
    assert manifest['count']==1
    assert len(manifest['invitations'])==1 and manifest['invitations'][0]['company_id']=='valid'
    assert manifest['excluded']==json.loads(batch['excluded'])
    assert manifest['excluded']==[{'candidate_id':'invalid','company':'GeoAIResume',
        'reasons':['Missing V1 field: business_description']}]
    retry=generate(db,{'ids':['valid','invalid']},key)
    assert retry['id']==batch['id'] and retry['package']==batch['package']
    assert count(db,batches)==1 and count(db,invitations)==1
    with db.transaction() as c:
        assert c.scalar(select(invitations.c.reference).where(invitations.c.candidate_id=='valid'))==manifest['invitations'][0]['invitation_reference']

def test_all_invalid_invitation_batch_rolls_back_without_partial_records(db):
    candidate(db,'invalid-only',personalization=False)
    with pytest.raises(ValueError,match='No eligible invitations'):
        generate(db,{'ids':['invalid-only']},'all-invalid-invitation-request')
    assert count(db,batches)==0 and count(db,invitations)==0

def test_authenticated_admin_generates_consistent_isolated_mixed_candidate_package(db,monkeypatch):
    from copy import deepcopy
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    from openpyxl import load_workbook
    from outreach_v2.app import create_app
    candidate(db,'package-valid','yuxuan.chen.1031@outlook.com',name='Brace Company',website='https://geo-ai-resume.vercel.app')
    candidate(db,'package-invalid','invalid@ordinary.example',name='Incomplete Company',website='https://ordinary.example',fixture=False)
    valid_personalization={'data_classification':'OBSERVED','business_description':'The page documents {"mode": "public"}.',
        'company_strength':'It presents {verified examples}.','source_urls':['https://geo-ai-resume.vercel.app'],
        'retrieved_at':now(),'recommendations':[{'title':'Observed {schema} example','problem':'The page shows {field: value}.',
            'suggestion':'Keep the {field} label consistent.','source_urls':['https://geo-ai-resume.vercel.app'],'observed_at':now()}]}
    with db.transaction() as c:
        c.execute(candidates.update().where(candidates.c.id=='package-valid').values(personalization=dump(valid_personalization)))
        c.execute(candidates.update().where(candidates.c.id=='package-invalid').values(personalization=dump({
            'data_classification':'OBSERVED','company_strength':'Observed only.','source_urls':['https://ordinary.example'],
            'retrieved_at':now(),'recommendations':[{'title':'One observed item','problem':'Observed problem.',
                'suggestion':'Possible correction.','source_urls':['https://ordinary.example'],'observed_at':now()}]})))
    before={t.name:count(db,t) for t in (invitations,batches,submissions,jobs)}
    monkeypatch.setenv('OUTREACH_ADMIN_PASSWORD_HASH',PasswordHasher().hash('local-only-test-password'))
    monkeypatch.setenv('OUTREACH_COOKIE_SECURE','false')
    client=TestClient(create_app(db));login=client.post('/api/auth/login',json={
        'email':'chen.yuxuan9@northeastern.edu','password':'local-only-test-password'})
    assert login.status_code==200 and login.json()['role']=='ADMIN'
    response=client.post('/api/batches',headers={'X-CSRF-Token':login.json()['csrf'],'Idempotency-Key':'isolated-api-package-key-123'},
        json={'ids':['package-valid','package-invalid']})
    assert response.status_code==200
    package=zipfile.ZipFile(io.BytesIO(response.content));manifest=json.loads(package.read('manifest.json'))
    assert manifest['count']==1 and manifest['excluded'][0]['candidate_id']=='package-invalid'
    assert manifest['excluded'][0]['reasons']==['Missing V1 field: business_description']
    csv_rows=list(csv.DictReader(io.StringIO(package.read('invitations.csv').decode('utf-8-sig'))))
    workbook=load_workbook(io.BytesIO(package.read('invitations.xlsx')))
    assert list(csv_rows[0])==[cell.value for cell in workbook.active[1]]
    assert list(csv_rows[0].values())==[cell.value for cell in workbook.active[2]]
    assert csv_rows[0]['company_name']=='Brace Company'
    assert '{"mode": "public"}' in csv_rows[0]['email_body'] and '{field: value}' in csv_rows[0]['email_body']
    assert manifest['invitations'][0]['invitation_id']==csv_rows[0]['invitation_id']
    assert manifest['invitations'][0]['invitation_reference']==csv_rows[0]['invitation_reference']
    from docx import Document
    preview=Document(io.BytesIO(package.read('personalized-previews.docx')))
    preview_text='\n'.join(p.text for p in preview.paragraphs)
    assert '{"mode": "public"}' in preview_text and csv_rows[0]['invitation_reference'] in preview_text
    merge=zipfile.ZipFile(io.BytesIO(package.read('invitation-mail-merge.docx')))
    assert 'word/document.xml' in merge.namelist()
    assert manifest['excluded']==json.loads(package.read('manifest.json'))['excluded']
    after={t.name:count(db,t) for t in (invitations,batches,submissions,jobs)}
    assert after=={'invitations':1,'batches':1,'submissions':0,'jobs':0}
    assert all(after[t.name]-before[t.name] == (1 if t in (invitations,batches) else 0) for t in (invitations,batches,submissions,jobs))
