import json
import pytest

from sqlalchemy import event, select, func, text

from outreach_v2.db import Database, candidates, contacts, invitations, now, SCHEMA
from outreach_v2.migrate import dump
from outreach_v2.service import list_candidates, generate, preview_generation


class _Result:
    def __init__(self, row=None): self.row=row
    def first(self): return self.row


class _IndexMigrationConnection:
    def __init__(self, migration_row, index_row):
        self.migration_row=migration_row;self.index_row=index_row;self.sql=[]
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def execution_options(self,**kwargs): return self
    def execute(self,statement,params=None):
        sql=str(statement);self.sql.append(sql)
        if 'pg_advisory_lock' in sql or 'pg_advisory_unlock' in sql:return _Result()
        if 'pg_class index_name' in sql:return _Result(self.index_row)
        if 'schema_migrations.version' in sql:return _Result(self.migration_row)
        return _Result()


class _IndexMigrationEngine:
    def __init__(self,connection): self.connection=connection
    def connect(self): return self.connection


def test_postgres_v12_repairs_missing_index_even_when_version_row_exists():
    connection=_IndexMigrationConnection(migration_row=(12,),index_row=None)
    database=Database.__new__(Database);database.postgres=True
    database.engine=_IndexMigrationEngine(connection)
    database._apply_online_candidate_index_migration()
    create=next(sql for sql in connection.sql if 'CREATE INDEX CONCURRENTLY' in sql)
    assert 'IF NOT EXISTS ix_candidates_name_id' in create
    assert f'ON {SCHEMA}.candidates (name, id)' in create
    assert not f'{SCHEMA}.ix_candidates_name_id' in create.split(' ON ')[0]
    assert f'ON {SCHEMA}.candidates (name, id)' in create
    assert not any('INSERT INTO' in sql and 'schema_migrations' in sql for sql in connection.sql)


def test_postgres_v12_records_existing_valid_index_without_rebuilding():
    connection=_IndexMigrationConnection(migration_row=None,index_row=type(
        'IndexState',(),{'indisvalid':True,'indisready':True})())
    database=Database.__new__(Database);database.postgres=True
    database.engine=_IndexMigrationEngine(connection)
    database._apply_online_candidate_index_migration()
    assert not any('CREATE INDEX CONCURRENTLY' in sql for sql in connection.sql)
    assert any('INSERT INTO' in sql and 'schema_migrations' in sql for sql in connection.sql)


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('OUTREACH_RESEARCHER_NAME','Performance Test')
    monkeypatch.setenv('OUTREACH_UNIVERSITY_AFFILIATION','Test University')
    monkeypatch.setenv('OUTREACH_REPLY_TO','researcher@example.edu')
    result=Database('sqlite:///'+str(tmp_path/'performance.db'))
    result.initialize()
    return result


def seed_candidate(db, ident):
    website=f'https://{ident}.performance.org'
    excerpt=(f'{ident} provides online tools for people organizing professional materials. '
             'Its pages explain the product workflow, intended users, and available features.')
    personalization={
        'data_classification':'OBSERVED','analysis_version':'structured-evidence-v2',
        'pipeline_version':'semantic-openai-v1',
        'business_description':excerpt,
        'company_strength':f'{ident} describes its online workflow and tools for organizing professional materials.',
        'recipient_copy':{'business_summary':f'{ident} describes online tools for professional materials.',
            'distinctive_detail':f'{ident} explains a workflow for organizing materials.',
            'research_relevance':'This supports a bounded study of how AI search summarizes a stated product purpose.',
            'supported_observation':'','support':{'business_summary':['main'],'distinctive_detail':['main'],
                'research_relevance':['main']}},
        'claim_support':{'business_description':['main'],'company_strength':['main']},
        'evidence_profile':{'pages':[{'url':website,'type':'home_or_other','title':ident,'headings':[],
                'truncated':False}],
            'facts':[{'id':'main','statement':excerpt,'evidence_excerpt':excerpt,'source_urls':[website],
                'assertion_type':'FIRST_PARTY_PAGE_CLAIM','attribution':'COMPANY_STATED'}]},
        'source_urls':[website],'retrieved_at':now(),'evidence':{'visible_text_excerpt':excerpt},
        'recommendations':[],'assessment':{'geo_observation':'NOT_ESTABLISHED'}}
    with db.transaction() as c:
        c.execute(candidates.insert().values(id=ident,kind='company',name=ident,website=website,
            domain=f'{ident}.performance.org',industry='Software',location='US',website_status='VERIFIED',
            qualification='ELIGIBLE',geography='US',suppressed=False,source='isolated-perf-test',
            personalization=dump(personalization),personalization_status='READY',
            personalization_quality_status='PASS',personalization_quality_errors='[]',
            personalization_version=1,personalization_analysis_version='semantic-openai-v1',
            fixture=False,created_at=now(),updated_at=now()))
        c.execute(contacts.insert().values(id='contact-'+ident,candidate_id=ident,kind='EMAIL',
            email=f'contact@{ident}.performance.org',source_url=website,
            evidence=f'Synthetic first-party benchmark evidence lists contact@{ident}.performance.org for general business inquiries',
            verification='VERIFIED',purpose='general business',verified_at=now(),
            verification_reason='synthetic benchmark fixture',authorization_reason='exact email in first-party fixture evidence',suppressed=False))


def test_candidate_listing_query_count_is_constant_across_page_size(db):
    for i in range(60):seed_candidate(db,f'company-{i:03d}')
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine,'before_cursor_execute',capture)
    try:
        small=list_candidates(db,{},page_size=10)
        small_count=len(statements);statements.clear()
        large=list_candidates(db,{},page_size=50)
        large_count=len(statements)
    finally:event.remove(db.engine,'before_cursor_execute',capture)
    assert len(small['rows'])==10 and len(large['rows'])==50
    assert small_count==large_count
    assert large_count<=7
    assert all('personalization' not in row for row in large['rows'])
    assert large['rows'][0]['contacts'][0]['email']=='contact@company-000.performance.org'


def test_candidate_listing_uses_stable_keyset_cursor_and_order_index(db):
    for i in range(25):seed_candidate(db,f'company-{i:03d}')
    first=list_candidates(db,{},page_size=10)
    second=list_candidates(db,{},page_size=10,cursor=first['next_cursor'])
    third=list_candidates(db,{},page_size=10,cursor=second['next_cursor'])
    ids=[row['id'] for page in (first,second,third) for row in page['rows']]
    assert len(ids)==25 and len(set(ids))==25
    assert ids==[f'company-{i:03d}' for i in range(25)]
    assert third['next_cursor'] is None
    with db.transaction() as c:
        plan=' '.join(str(row) for row in c.execute(text(
            'EXPLAIN QUERY PLAN SELECT id,name FROM candidates ORDER BY name,id LIMIT 50')))
    assert 'ix_candidates_name_id' in plan


def test_candidate_listing_rejects_malformed_cursor(db):
    with pytest.raises(ValueError,match='Invalid candidate pagination cursor'):
        list_candidates(db,{},cursor='not-a-valid-cursor')


def test_150_candidate_generation_uses_batched_reads_and_keeps_review_only_output(db):
    for i in range(150):seed_candidate(db,f'business-{i:03d}')
    selection={'ids':[f'business-{i:03d}' for i in range(150)]}
    selection['preview_sha256']=preview_generation(db,selection)['preview_sha256']
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
    event.listen(db.engine,'before_cursor_execute',capture)
    try:
        batch=generate(db,selection,
            'perf-batch-request-key-150-candidates')
    finally:event.remove(db.engine,'before_cursor_execute',capture)
    with db.transaction() as c:
        invitation_count=c.scalar(select(func.count()).select_from(invitations))
    assert invitation_count==150
    assert batch['pilot_review_status']=='PENDING_REVIEW'
    assert len(statements)<=30
    assert 'package' in batch and batch['package']
