import csv,io,json,zipfile,sqlite3,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from sqlalchemy import select,func
from outreach_v2.db import Database,candidates,contacts,invitations,batches,jobs,submissions,deliveries,reports,sessions,now
from outreach_v2.service import generate,list_candidates
from outreach_v2.consent import process
from outreach_v2.template import render,BULK_SUBJECT
from outreach_v2.migrate import Migrator,dump
from outreach_v2.worker import tick
from outreach_v2.integrations import ConfigurationBlocked,OutcomeUncertain

@pytest.fixture
def db(tmp_path):
    result=Database('sqlite:///'+str(tmp_path/'test.db'));result.initialize();return result

def candidate(db,ident='a',email='yuxuan.chen.1031@outlook.com',website='https://geo-ai-resume.vercel.app',name='GeoAIResume',suppressed=False,personalization=True):
    p={'data_classification':'OBSERVED','business_description':'Resume building website.','company_strength':'The page describes resume building features.',
       'source_urls':[website],'retrieved_at':now(),'recommendations':[{'title':'Page title','problem':'The sampled page has no title.','suggestion':'Add a descriptive page title.','source_urls':[website],'observed_at':now()}]}
    with db.transaction() as c:
        c.execute(candidates.insert().values(id=ident,kind='company',name=name,website=website,domain=website.split('//')[1],industry='Software',location='Unknown',website_status='VERIFIED',qualification='ELIGIBLE',geography='UNKNOWN',suppressed=suppressed,source='isolated-unit-test',personalization=dump(p if personalization else {}),personalization_status='READY' if personalization else 'INCOMPLETE',fixture=True,created_at=now(),updated_at=now()))
        c.execute(contacts.insert().values(id='contact-'+ident,candidate_id=ident,kind='EMAIL',email=email,source_url=website,verification='OWNER_AUTHORIZED',suppressed=False))
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
    candidate(db);candidate(db,'b','yuxaun.chen.1031@outlook.com','https://whisker-haven-demo.vercel.app','Whisker Haven')
    batch=invite(db,['a','b']);z=zipfile.ZipFile(io.BytesIO(batch['package']))
    rows=list(csv.DictReader(io.StringIO(z.read('invitations.csv').decode('utf-8-sig'))))
    assert len(rows)==2
    assert [(r['company_name'],r['recipient_email']) for r in rows]==[('GeoAIResume','yuxuan.chen.1031@outlook.com'),('Whisker Haven','yuxaun.chen.1031@outlook.com')]
    from openpyxl import load_workbook
    workbook=load_workbook(io.BytesIO(z.read('invitations.xlsx')))
    assert list(rows[0])==[c.value for c in workbook.active[1]]
    xml=zipfile.ZipFile(io.BytesIO(z.read('invitation-mail-merge.docx'))).read('word/document.xml').decode()
    import re
    fields=re.findall('MERGEFIELD (body_paragraph_\\d+)',xml)
    assert fields and set(fields)<=set(rows[0])
    for r in rows:
        assert r['invitation_reference'] in r['email_body'] and r['questionnaire_url'] in r['email_body']
        assert r['bulk_subject']==BULK_SUBJECT
        assert r['Email Address']==r['recipient_email']
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

def test_unknown_reference_and_company_scope_block(db):
    candidate(db);invite(db);row=response(db);row['Invitation Reference']='GEO-INVALID'
    assert process(db,row)=='BLOCKED_REFERENCE';assert count(db,jobs)==0
    row=response(db);row['Business Website']='https://other.example'
    assert process(db,row)=='BLOCKED_SCOPE';assert count(db,jobs)==0

def test_owner_consent_is_automatic_and_duplicate_safe(db):
    candidate(db);invite(db);row=response(db)
    assert process(db,row)=='AUTHORIZED';assert count(db,jobs)==1
    assert process(db,row)=='DUPLICATE';row['Timestamp']='10/27/2026 22:31:00'
    assert process(db,row)=='DUPLICATE_CONSENT';assert count(db,jobs)==1

def test_foreign_representative_does_not_redirect_report(db):
    candidate(db);invite(db);row=response(db,email='someone@other.example')
    assert process(db,row)=='BLOCKED_IDENTITY';assert count(db,jobs)==0
    with db.transaction() as c:assert c.scalar(select(invitations.c.recipient))=='yuxuan.chen.1031@outlook.com'

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
