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


def test_research_step_runs_advance_only_for_configured_job(monkeypatch):
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_JOB_ID", KEY)
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_ENABLED", "true")
    inv = {"id": "inv-existing", "candidate_id": "candidate-existing", "website": SITE,
           "company_name": "GeoAIResume", "recipient": "researcher@example.test",
           "reference": "GEO-existing", "consent_status": "AUTHORIZED"}
    candidate = {"fixture": True, "kind": "company", "industry": "research", "location": "test"}
    job = {"id": "job-existing", "idempotency_key": KEY, "submission_id": "submission-existing",
           "started_at": None, "property_id": None}
    monkeypatch.setattr(worker, "context", lambda _c, _j: (inv, candidate))
    transitions = []
    monkeypatch.setattr(worker, "transition", lambda _c, _id, **values: transitions.append(values))
    monkeypatch.setattr(worker, "now", lambda: "2026-11-05T00:00:00+00:00")

    class Engine:
        calls = []
        def reconcile(self, key):
            self.calls.append(("reconcile", key))
            return None
        def start(self, key, payload):
            self.calls.append(("start", key, payload))
            return {"status": "QUEUED", "job_id": "engine-existing"}
        def advance(self, key):
            self.calls.append(("advance", key))
            return {"status": "BLOCKED_CONFIGURATION", "job_id": "engine-existing"}

    engine = Engine()
    worker.research_step(DummyDB(), job, lambda: engine)
    assert [call[0] for call in engine.calls] == ["reconcile", "start", "advance"]
    payload = engine.calls[1][2]
    assert payload["identity"]["website"] == SITE
    assert payload["consent"]["research_interest"] == payload["consent"]["research_authorization"] == "YES"
    assert transitions[0]["status"] == "BLOCKED_CONFIGURATION"


def test_research_step_rejects_other_job_before_engine_access(monkeypatch):
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_JOB_ID", KEY)
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_ENABLED", "true")
    monkeypatch.setattr(worker, "context", lambda _c, _j: ({"website": SITE}, {}))
    class Engine:
        def __init__(self):
            raise AssertionError("Engine must not be contacted for another job")
    with pytest.raises(ConfigurationBlocked, match="configured GeoAIResume"):
        worker.research_step(DummyDB(), {"idempotency_key": "outreach-v2:inv-other"}, Engine)


def test_report_delivery_is_off_by_default_and_never_constructs_sender(monkeypatch):
    monkeypatch.delenv("OUTREACH_REPORT_DELIVERY_ENABLED", raising=False)
    assert worker.report_delivery_enabled() is False
    monkeypatch.setenv("OUTREACH_REPORT_DELIVERY_ENABLED", "false")
    assert worker.report_delivery_enabled() is False
    monkeypatch.setenv("OUTREACH_REPORT_DELIVERY_ENABLED", "true")
    assert worker.report_delivery_enabled() is True


def test_claim_reopens_only_the_allowlisted_quarantined_job(tmp_path, monkeypatch):
    from outreach_v2.db import Database, batches, candidates, invitations, jobs, submissions
    monkeypatch.setenv("GEO_OUTREACH_RESEARCH_JOB_ID", KEY)
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
    assert claimed["id"] == "job-85981437cfa04f779348de67"
    assert claimed["stage"] == "RESEARCH"
    with db.transaction() as conn:
        assert conn.scalar(select(jobs.c.stage).where(jobs.c.id==claimed["id"])) == "RESEARCH"
        other=conn.execute(select(jobs).where(jobs.c.id=="job-other")).mappings().one()
        assert other["attempts"] == 39 and other["lease_token"] is None
