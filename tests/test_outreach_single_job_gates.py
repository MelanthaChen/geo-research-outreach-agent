from contextlib import contextmanager
import pytest
from sqlalchemy import select

from outreach_v2 import worker
from outreach_v2.integrations import ConfigurationBlocked


KEY = "outreach-v2:inv-9880a65388184eafa7c1075f7bbadb83"
SITE = "https://geo-ai-resume.vercel.app/"


class DummyDB:
    @contextmanager
    def transaction(self, serialized=False):
        yield object()


def test_research_step_requires_new_single_key_allowlist_and_exact_fresh_candidate(monkeypatch):
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_JOB_ALLOWLIST", "outreach-v2:inv-fresh-authorized-job")
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_ENABLED", "true")
    inv = {"id": "inv-existing", "candidate_id": "candidate-existing", "website": SITE,
           "company_name": "GeoAIResume", "recipient": "researcher@example.test",
           "reference": "GEO-existing", "consent_status": "AUTHORIZED"}
    candidate = {"id":"previous-candidate","fixture": True, "kind": "company", "industry": "research", "location": "test"}
    job = {"id": "job-existing", "idempotency_key": KEY, "submission_id": "submission-existing",
           "started_at": None, "property_id": None}
    monkeypatch.setattr(worker, "context", lambda _c, _j: (inv, candidate))
    class Engine:
        def __init__(self):pytest.fail('Engine must not be contacted for a non-selected fresh candidate')
    with pytest.raises(ConfigurationBlocked,match='exact configured job allowlist'):
        worker.research_step(DummyDB(), job, Engine)


def test_research_step_rejects_other_job_before_engine_access(monkeypatch):
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_JOB_ALLOWLIST", KEY)
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_ENABLED", "true")
    monkeypatch.setattr(worker, "context", lambda _c, _j: ({"website": SITE}, {}))
    class Engine:
        def __init__(self):
            raise AssertionError("Engine must not be contacted for another job")
    with pytest.raises(ConfigurationBlocked, match="exact configured job allowlist"):
        worker.research_step(DummyDB(), {"idempotency_key": "outreach-v2:inv-other"}, Engine)


def test_report_delivery_is_off_by_default_and_never_constructs_sender(monkeypatch):
    monkeypatch.delenv("OUTREACH_REPORT_DELIVERY_ENABLED", raising=False)
    assert worker.report_delivery_enabled() is False
    monkeypatch.setenv("OUTREACH_REPORT_DELIVERY_ENABLED", "false")
    assert worker.report_delivery_enabled() is False
    monkeypatch.setenv("OUTREACH_REPORT_DELIVERY_ENABLED", "true")
    assert worker.report_delivery_enabled() is True


def test_fresh_allowlist_never_reopens_previous_completed_quarantined_job(tmp_path, monkeypatch):
    from outreach_v2.db import Database, batches, candidates, invitations, jobs, submissions
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_JOB_ALLOWLIST", "outreach-v2:inv-fresh-authorized-job")
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_ENABLED", "true")
    db = Database(f"sqlite:///{tmp_path / 'claim.sqlite'}")
    db.initialize()
    with db.transaction() as conn:
        conn.execute(candidates.insert().values(id="c-existing",kind="company",name="GeoAIResume",
            website=SITE,domain="geo-ai-resume.vercel.app",suppressed=False,fixture=True,
            created_at="2026-11-05T00:00:00+00:00",updated_at="2026-11-05T00:00:00+00:00"))
        conn.execute(batches.insert().values(id="b-existing",request_key="r-existing",selection_hash="h-existing"))
        conn.execute(invitations.insert().values(id="i-existing",candidate_id="c-existing",batch_id="b-existing",
            reference="GEO-existing",recipient="researcher@example.test",website=SITE,company_name="GeoAIResume"))
        conn.execute(submissions.insert().values(id="s-existing",invitation_id="i-existing",reference="GEO-existing"))
        conn.execute(jobs.insert().values(id="job-85981437cfa04f779348de67",invitation_id="i-existing",
            submission_id="s-existing",status="BLOCKED_CONFIGURATION",stage="DONE",idempotency_key=KEY,
            attempts=2,created_at="2026-11-05T00:00:00+00:00"))
        conn.execute(candidates.insert().values(id="c-other",kind="company",name="Unrelated queued company",
            website="https://unrelated.example/",domain="unrelated.example",suppressed=False,fixture=False,
            created_at="2026-10-27T00:00:00+00:00",updated_at="2026-10-27T00:00:00+00:00"))
        conn.execute(invitations.insert().values(id="i-other",candidate_id="c-other",batch_id="b-existing",
            reference="GEO-other",recipient="other@example.test",website="https://unrelated.example/",company_name="Unrelated"))
        conn.execute(submissions.insert().values(id="s-other",invitation_id="i-other",reference="GEO-other"))
        conn.execute(jobs.insert().values(id="job-other",invitation_id="i-other",submission_id="s-other",
            status="RETRY_PENDING",stage="RESEARCH",idempotency_key="outreach-v2:inv-other",attempts=39,
            next_attempt_at="2026-10-27T00:00:00+00:00",created_at="2026-10-27T00:00:00+00:00"))
    claimed = worker.claim(db)
    assert claimed is None
    with db.transaction() as conn:
        old=conn.execute(select(jobs).where(jobs.c.id=="job-85981437cfa04f779348de67")).mappings().one()
        other=conn.execute(select(jobs).where(jobs.c.id=="job-other")).mappings().one()
        assert old["attempts"]==2 and old["stage"]=="DONE" and old["lease_token"] is None
        assert other["attempts"] == 39 and other["lease_token"] is None


def test_exact_job_claim_does_not_mutate_old_delivery_or_unrelated_queue(tmp_path,monkeypatch):
    from outreach_v2.db import Database,batches,candidates,invitations,jobs,submissions
    from outreach_v2.migrate import dump
    from outreach_v2.db import now
    db=Database(f"sqlite:///{tmp_path/'exact-claim.sqlite'}");db.initialize()
    with db.transaction() as c:
        c.execute(batches.insert().values(id='b',request_key='request-key-exact-0001',selection_hash='hash'))
        for suffix,stage,status in [('old','DELIVERY','COMPLETED'),('target','RESEARCH','QUEUED'),('other','RESEARCH','QUEUED')]:
            site='https://geo-ai-resume.vercel.app/' if suffix=='target' else f'https://{suffix}.example/'
            c.execute(candidates.insert().values(id='c-'+suffix,kind='company',name=suffix,website=site,
                domain=site.split('//')[1].strip('/'),suppressed=False,fixture=suffix=='target',created_at=now(),updated_at=now()))
            c.execute(invitations.insert().values(id='i-'+suffix,candidate_id='c-'+suffix,batch_id='b',
                reference='GEO-'+suffix,recipient=f'{suffix}@example.test',website=site,company_name=suffix,consent_status='AUTHORIZED'))
            c.execute(submissions.insert().values(id='s-'+suffix,invitation_id='i-'+suffix,reference='GEO-'+suffix,status='AUTHORIZED'))
            c.execute(jobs.insert().values(id='job-'+suffix,invitation_id='i-'+suffix,submission_id='s-'+suffix,
                status=status,stage=stage,idempotency_key='outreach-v2:inv-'+suffix,attempts=17 if suffix!='target' else 0,
                created_at=now(),next_attempt_at='2000-01-01T00:00:00+00:00'))
    with pytest.raises(ValueError,match='RESEARCH or REPORT'):
        worker.claim(db,job_id='job-target',idempotency_key='outreach-v2:inv-target')
    assert worker.claim(db,job_id='job-target',idempotency_key='wrong-key',stage='RESEARCH') is None
    claimed=worker.claim(db,job_id='job-target',idempotency_key='outreach-v2:inv-target',stage='RESEARCH')
    assert claimed['id']=='job-target'
    with db.transaction() as c:
        rows={r['id']:dict(r) for r in c.execute(select(jobs)).mappings()}
    assert rows['job-old']['attempts']==17 and rows['job-old']['lease_token'] is None and rows['job-old']['stage']=='DELIVERY'
    assert rows['job-other']['attempts']==17 and rows['job-other']['lease_token'] is None
    assert rows['job-target']['attempts']==1 and rows['job-target']['lease_token']==claimed['lease_token']


def test_report_only_claim_can_only_select_exact_report_stage(tmp_path):
    from outreach_v2.db import Database,batches,candidates,invitations,jobs,submissions,now
    db=Database(f"sqlite:///{tmp_path/'exact-report.sqlite'}");db.initialize()
    with db.transaction() as c:
        c.execute(batches.insert().values(id='b',request_key='request-key-report-0001',selection_hash='hash'))
        for suffix,stage in [('old','DELIVERY'),('target','REPORT')]:
            site=f'https://{suffix}.example/'
            c.execute(candidates.insert().values(id='c-'+suffix,kind='company',name=suffix,website=site,domain=f'{suffix}.example',suppressed=False,created_at=now(),updated_at=now()))
            c.execute(invitations.insert().values(id='i-'+suffix,candidate_id='c-'+suffix,batch_id='b',reference='GEO-'+suffix,recipient=f'{suffix}@example.test',website=site,company_name=suffix,consent_status='AUTHORIZED'))
            c.execute(submissions.insert().values(id='s-'+suffix,invitation_id='i-'+suffix,reference='GEO-'+suffix,status='AUTHORIZED'))
            c.execute(jobs.insert().values(id='job-'+suffix,invitation_id='i-'+suffix,submission_id='s-'+suffix,status='COMPLETED',stage=stage,idempotency_key='outreach-v2:inv-'+suffix,attempts=4,created_at=now()))
    claimed=worker.claim(db,job_id='job-target',idempotency_key='outreach-v2:inv-target',stage='REPORT')
    assert claimed['id']=='job-target' and claimed['stage']=='REPORT'
    assert worker.claim(db,job_id='job-old',idempotency_key='outreach-v2:inv-old',stage='REPORT') is None
    with db.transaction() as c:
        old=c.execute(select(jobs).where(jobs.c.id=='job-old')).mappings().one()
    assert old['attempts']==4 and old['lease_token'] is None
