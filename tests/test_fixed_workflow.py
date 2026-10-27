"""Persisted integration tests with injected providers; these are not live evidence."""
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from outreach_agent.research_lifecycle import (_connect, register_profile, generate_invitation_batch,
    get_record, ingest_google_form_response)
from outreach_agent.geo_engine_handoff import OWNER_FIXTURES, process_geo_handoff
from outreach_agent.email_dispatch import send_invitation_batch, ProviderResult
from outreach_agent.geo_result_delivery import resume_accepted_research, follow_up_is_blocked
from outreach_agent.mail_merge import research_mail_merge_rows
from scripts.sync_google_form_responses import handoff_owner_test_fixtures


def seed(path, profile_id='geoairesume-owner-test-v1'):
    domain, email = OWNER_FIXTURES[profile_id]
    url = 'https://' + domain
    register_profile({'schema_version':'geo-company-profile/v1','profile_id':profile_id,'profile_type':'owner_controlled_test','identity':{'name':profile_id,'website':url,'verification_status':'VERIFIED'}}, path)
    payload={'data_classification':'OBSERVED','business_description':'Unit test website evidence','company_strength':'Unit test evidence of service details','source_urls':[url],'retrieved_at':'2026-10-27T12:00:00Z','recommendations':[{'title':'Unit test observation','problem':'Test page lacks a direct answer','suggestion':'Add an evidence-backed direct answer','source_urls':[url],'observed_at':'2026-10-27T12:00:00Z'}]}
    with _connect(path) as db:
        db.execute('UPDATE research_records SET personalization_json=? WHERE profile_id=?',(json.dumps(payload),profile_id))
        db.execute('INSERT INTO owner_test_recipient_bindings(profile_id,recipient,label,created_at,updated_at) VALUES (?,?,?,?,?)',(profile_id,email,'unit test','now','now'))
    return profile_id, email


def response(record, email, interest='Yes', authorization='Yes'):
    draft = record['email_drafts'][0]
    return {'Timestamp':'2026-10-27T12:01:00Z','Invitation Reference':draft['reference'],
        'Is your company interested in participating in this research?':interest,
        'Does your company authorize our research team to conduct the research described above?':authorization,
        'Full Name of the Authorized Company Representative':'Owner test',
        'Job Title of the Authorized Company Representative':'Owner',
        'Business Email Address of the Authorized Company Representative':email}


def row_for(path, pid, email, interest='Yes', authorization='Yes'):
    record=get_record(pid,path)
    with _connect(path) as db:
        record['email_drafts'][0]['reference']=db.execute('SELECT invitation_reference FROM questionnaire_invitations WHERE invitation_id=?',(record['email_drafts'][0]['invitation_id'],)).fetchone()[0]
    return response(record,email,interest,authorization)


class Provider:
    name='unit-test-provider'; enabled=True
    def __init__(self): self.calls=[]
    def send(self,**kwargs):
        self.calls.append(kwargs); return ProviderResult('test-message-'+str(len(self.calls)))
    def send_document(self,**kwargs): return self.send(**kwargs)


def test_two_company_batch_exports_same_content_and_send_is_idempotent(tmp_path):
    path=tmp_path/'registry.db'
    ids=[seed(path,pid)[0] for pid in OWNER_FIXTURES]
    first=generate_invitation_batch(ids,secret='test',path=path)
    second=generate_invitation_batch(ids,secret='test',path=path)
    assert first['draft_ids']==second['draft_ids'] and first['batch_id']==second['batch_id']
    rows=research_mail_merge_rows(path,draft_ids=first['draft_ids'],include_pending=True)
    assert len(rows)==2 and len({r['invitation_reference'] for r in rows})==2
    for row in rows:
        record=get_record(row['profile_id'],path)
        assert row['email_body']==record['email_drafts'][0]['body']
        assert row['recipient_email']==OWNER_FIXTURES[row['profile_id']][1]
        assert row['questionnaire_url'] in row['email_body']
        assert '{{' not in row['email_body']
    provider=Provider()
    one=send_invitation_batch(first['batch_id'],path=path,provider=provider,sender='test@example.test')
    two=send_invitation_batch(first['batch_id'],path=path,provider=provider,sender='test@example.test')
    assert all(o['status']=='SENT' for o in one['outcomes'])
    assert all(o['status']=='DUPLICATE' for o in two['outcomes'])
    assert len(provider.calls)==2
    assert generate_invitation_batch(ids,secret='test',path=path)['draft_ids']==first['draft_ids']


@pytest.mark.parametrize('interest,authorization',[('Yes','No'),('No','Yes'),('No','No'),('bad','Yes')])
def test_rejection_never_starts_research(tmp_path,monkeypatch,interest,authorization):
    monkeypatch.setenv('OUTREACH_OWNER_TEST_AUTOMATION_ENABLED','true')
    path=tmp_path/'registry.db';pid,email=seed(path)
    generate_invitation_batch([pid],secret='test',path=path)
    row=row_for(path,pid,email,interest,authorization)
    assert ingest_google_form_response(row,path=path)!='CONSENTED'
    assert ingest_google_form_response(row,path=path)=='DUPLICATE'
    assert resume_accepted_research(path=path)==[]
    assert get_record(pid,path)['handoffs']==[]


def test_missing_research_is_rejected_without_orphan_invitation(tmp_path):
    path=tmp_path/'registry.db';pid,email=seed(path)
    with _connect(path) as db: db.execute("UPDATE research_records SET personalization_json='{}' WHERE profile_id=?",(pid,))
    result=generate_invitation_batch([pid],secret='test',path=path)
    assert result['categories']['insufficient_verified_evidence'][0]['error'].startswith('observed personalization')
    with _connect(path) as db: assert db.execute('SELECT count(*) FROM questionnaire_invitations').fetchone()[0]==0


def test_yes_pair_resumes_after_restart_and_report_uses_original_recipient(tmp_path,monkeypatch):
    monkeypatch.setenv('OUTREACH_OWNER_TEST_AUTOMATION_ENABLED','true')
    path=tmp_path/'registry.db';pid,email=seed(path)
    generate_invitation_batch([pid],secret='test',path=path)
    row=row_for(path,pid,email)
    assert ingest_google_form_response(row,path=path)=='CONSENTED'
    # Ingestion only persists; the independent scheduler resumes it.
    assert get_record(pid,path)['handoffs']==[]
    class Bridge:
        runs=0
        def import_profile(self,profile): return {'property_id':91}
        def run_controlled_research(self,profile):
            self.runs+=1
            return {'status':'COMPLETED','result':{'record_id':'unit-result'}}
        def get_research_result(self,pid):
            return {'contract_version':'geo-research-integration/v1','schema_version':'geo-research-integration/v1','record_type':'research_result_export','source_profile_id':pid,'source_profile_version':'geo-company-profile/v1','property_id':91,'record_id':'unit-result','audit_reference':{},'strategies':[],'question_set_reference':None,'baseline_reference':None,'post_deployment_reference':None,'exported_at':'2026-10-27T12:05:00Z','provenance':{},'observed_pages':[{'url':'https://geo-ai-resume.vercel.app','exact_quotes':['Unit test source quote']}],'recommendations':[{'title':'Unit test recommendation'}],'baseline':{'provider':'UNIT_TEST_ONLY','model_calls':1,'results':['Unit test output']}}
    bridge=Bridge();provider=Provider()
    template=Path(__file__).parents[1]/'templates/company_geo_analysis_report_template.docx'
    one=resume_accepted_research(path=path,bridge=bridge,provider=provider,sender='test@example.test',report_directory=tmp_path/'reports',template=template)
    assert one[0]['status']=='SENT'
    # Distinct representative email stays separate; original remains delivery target.
    with _connect(path) as db: db.execute("UPDATE questionnaire_submissions SET representative_email='representative@example.test'")
    assert ingest_google_form_response(row,path=path)=='DUPLICATE'
    two=resume_accepted_research(path=path,bridge=bridge,provider=provider,sender='test@example.test',report_directory=tmp_path/'reports',template=template)
    assert two[0]['status']=='EXISTING' and bridge.runs==1
    assert len(provider.calls)==1 and provider.calls[0]['recipient']==email
    assert provider.calls[0]['content'].startswith(b'PK')
    with _connect(path) as db:
        report=db.execute('SELECT * FROM research_reports').fetchone()
        assert report['sent_at'] and report['document_base64']


def test_mismatched_site_or_representative_cannot_auto_consent(tmp_path,monkeypatch):
    monkeypatch.setenv('OUTREACH_OWNER_TEST_AUTOMATION_ENABLED','true')
    path=tmp_path/'registry.db';pid,email=seed(path)
    generate_invitation_batch([pid],secret='test',path=path)
    assert ingest_google_form_response(row_for(path,pid,'attacker@example.test'),path=path)=='NEEDS_REVIEW'


def test_malformed_timestamp_cannot_start_research(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTREACH_OWNER_TEST_AUTOMATION_ENABLED','true')
    path=tmp_path/'registry.db'; pid,email=seed(path)
    generate_invitation_batch([pid],secret='test',path=path)
    row=row_for(path,pid,email); row['Timestamp']='not a date'
    assert ingest_google_form_response(row,path=path)=='NEEDS_REVIEW'
    assert resume_accepted_research(path=path)==[]


def test_scheduler_marks_failed_handoffs_blocked():
    assert follow_up_is_blocked([{"status": "HANDOFF_FAILED"}])
    assert follow_up_is_blocked([{"status": "INCOMPLETE"}])
    assert not follow_up_is_blocked([{"status": "SENT"}, {"status": "EXISTING"}])
    assert not follow_up_is_blocked([])


def test_handoff_only_retry_reuses_failed_rows_without_starting_research(tmp_path, monkeypatch):
    monkeypatch.setenv('OUTREACH_OWNER_TEST_AUTOMATION_ENABLED', 'true')
    path=tmp_path/'registry.db'
    profiles=[seed(path,pid) for pid in OWNER_FIXTURES]
    generate_invitation_batch([pid for pid,_ in profiles],secret='test',path=path)
    for pid,email in profiles:
        assert ingest_google_form_response(row_for(path,pid,email),path=path)=='CONSENTED'

    class TimeoutBridge:
        def import_profile(self,profile): raise TimeoutError('simulated prior timeout')
    for pid,_ in profiles:
        submission_id=get_record(pid,path)['questionnaire_submissions'][0]['submission_id']
        assert process_geo_handoff(submission_id,path=path,bridge=TimeoutBridge())['status']=='HANDOFF_FAILED'

    class ExistingEngineBridge:
        def __init__(self): self.calls=[]
        def import_profile(self,profile):
            self.calls.append(profile['profile_id'])
            return {'property_id': {'geoairesume-owner-test-v1': 21,'whisker-haven-owner-demo-20261023-v1': 22}[profile['profile_id']]}
        def run_controlled_research(self,profile): raise AssertionError('handoff-only retry must not start research')
    bridge=ExistingEngineBridge()
    monkeypatch.setattr('outreach_agent.geo_engine_handoff.default_bridge',lambda:bridge)
    first=handoff_owner_test_fixtures(path=path)
    assert first['status']=='COMPLETED'
    assert {x['profile_id']:x['property_id'] for x in first['handoffs']}=={'geoairesume-owner-test-v1':21,'whisker-haven-owner-demo-20261023-v1':22}
    second=handoff_owner_test_fixtures(path=path)
    assert second['status']=='COMPLETED'
    assert all(x['idempotent'] for x in second['handoffs']) and len(bridge.calls)==2
    assert all(get_record(pid,path)['geo_research_status']=='HANDED_OFF' for pid,_ in profiles)
    assert all(get_record(pid,path)['handoffs'][0]['research_run_status']=='NOT_STARTED' for pid,_ in profiles)


def test_postgres_schema_initialization_retries_failure_then_reuses_success(monkeypatch):
    from outreach_agent import research_lifecycle as lifecycle
    target='postgresql://schema-cache-unit-test/registry'
    lifecycle._initialized_postgres_targets.discard(target)
    calls=[]
    def initialize(path):
        calls.append(path)
        if len(calls)==1:
            raise RuntimeError('migration failed')
        return path
    monkeypatch.setattr(lifecycle,'_initialize_registry',initialize)
    with pytest.raises(RuntimeError): lifecycle.init_registry(target)
    assert lifecycle.init_registry(target)==target
    assert lifecycle.init_registry(target)==target
    assert len(calls)==2
    lifecycle._initialized_postgres_targets.discard(target)
