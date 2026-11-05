from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import json

import pytest
from docx import Document
from sqlalchemy import select, func

from outreach_v2 import worker
from outreach_v2.db import (
    Database, batches, candidates, events, invitations, jobs, report_reviews,
    reports, schema_migrations, submissions, admin_users,
)
from outreach_v2.integrations import ConfigurationBlocked
from outreach_v2.migrate import dump


JOB_ID = "job-report-version-test"
KEY = "outreach-v2:inv-report-version-test"
INV_ID = "inv-report-version-test"
SITE = "https://geo-ai-resume.vercel.app/"
RESULT_ID = "engine-result-genuine-6"
PREVIEW_ID = "simulated-result-TEST-ONLY-NO-ENGINE-RESULT"


def make_docx(text):
    document = Document()
    document.add_paragraph(text)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def genuine_result():
    return {
        "record_id": RESULT_ID,
        "contract_version": "geo-research-integration/v1",
        "source_profile_id": "candidate-geo",
        "source_profile_version": INV_ID,
        "property_id": "2",
        "observed_pages": [{"url": SITE, "title": "GeoAIResume"}],
        "recommendations": [{"title": "Improve clear product descriptions"}],
        "baseline": {
            "provider": "openai",
            "model": "gpt-6-luna",
            "model_calls": 2,
            "results": [{"text": "Actual provider-generated analysis"}],
            "response_archive": "experiment:6",
        },
    }


def setup_db(tmp_path, *, preview=False, other=False):
    db = Database(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    db.initialize()
    result = genuine_result()
    old_doc = make_docx("TEST-ONLY simulated preview; NO-ENGINE-RESULT")
    with db.transaction() as c:
        c.execute(candidates.insert().values(id="candidate-geo", kind="company", name="GeoAIResume",
            website=SITE, domain="geo-ai-resume.vercel.app", fixture=True, suppressed=False,
            created_at="2026-11-05T00:00:00+00:00", updated_at="2026-11-05T00:00:00+00:00"))
        c.execute(batches.insert().values(id="batch-geo", request_key="batch-request-geo", selection_hash="batch-hash-geo"))
        c.execute(invitations.insert().values(id=INV_ID, candidate_id="candidate-geo", batch_id="batch-geo",
            reference="GEO-REPORT-TEST", recipient="owner@example.test", website=SITE,
            company_name="GeoAIResume", consent_status="AUTHORIZED"))
        c.execute(submissions.insert().values(id="submission-geo", invitation_id=INV_ID, reference="GEO-REPORT-TEST"))
        c.execute(jobs.insert().values(id=JOB_ID, invitation_id=INV_ID, submission_id="submission-geo",
            status="COMPLETED", stage="REPORT", idempotency_key=KEY, property_id="2",
            result_id=RESULT_ID, result=dump(result), lease_token="lease-geo", created_at="2026-11-05T00:00:00+00:00"))
        if preview:
            c.execute(reports.insert().values(id="report-preview-TEST-ONLY", job_id=JOB_ID,
                recipient="owner@example.test", result_id=PREVIEW_ID, document=old_doc,
                sha256=sha256(old_doc).hexdigest(), created_at="2026-11-05T00:00:00+00:00"))
            c.execute(report_reviews.insert().values(id="review-preview-approved", report_id="report-preview-TEST-ONLY",
                document_sha256=sha256(old_doc).hexdigest(), decision="APPROVED",
                reviewer_email="professor@example.edu", reviewed_at="2026-11-05T00:01:00+00:00", note="Approved old preview"))
        if other:
            c.execute(candidates.insert().values(id="candidate-other", kind="company", name="Other Company",
                website="https://other.example/", domain="other.example", fixture=False, suppressed=False,
                created_at="2026-11-05T00:00:00+00:00", updated_at="2026-11-05T00:00:00+00:00"))
            c.execute(invitations.insert().values(id="inv-other", candidate_id="candidate-other", batch_id="batch-geo",
                reference="GEO-OTHER", recipient="other@example.test", website="https://other.example/",
                company_name="Other Company", consent_status="AUTHORIZED"))
            c.execute(submissions.insert().values(id="submission-other", invitation_id="inv-other", reference="GEO-OTHER"))
            c.execute(jobs.insert().values(id="job-other", invitation_id="inv-other", submission_id="submission-other",
                status="COMPLETED", stage="REPORT", idempotency_key="outreach-v2:inv-other", property_id="9",
                result_id="other-result", result="{}", lease_token="lease-other", created_at="2026-11-05T00:00:00+00:00"))
            other_doc = make_docx("Other unchanged report")
            c.execute(reports.insert().values(id="report-other", job_id="job-other", recipient="other@example.test",
                result_id="other-result", document=other_doc, sha256=sha256(other_doc).hexdigest(),
                created_at="2026-11-05T00:00:00+00:00"))
    with db.transaction() as c:
        job = dict(c.execute(select(jobs).where(jobs.c.id == JOB_ID)).mappings().one())
    return db, job, old_doc


def renderer(result):
    assert result["record_id"] == RESULT_ID
    return make_docx(f"Genuine research {result['record_id']} {SITE}")


def report_rows(db, job_id=JOB_ID):
    with db.transaction() as c:
        return [dict(row) for row in c.execute(select(reports).where(reports.c.job_id == job_id)).mappings()]


def test_matching_genuine_report_is_reused_idempotently(tmp_path):
    db, job, _ = setup_db(tmp_path)
    doc = renderer(genuine_result())
    report_id = "report-existing-genuine"
    with db.transaction() as c:
        c.execute(reports.insert().values(id=report_id, job_id=JOB_ID, recipient="owner@example.test",
            result_id=RESULT_ID, document=doc, sha256=sha256(doc).hexdigest(), created_at="2026-11-05T00:00:00+00:00"))
    worker.report_step(db, job, renderer=lambda _result: pytest.fail("matching genuine report was rerendered"))
    with db.transaction() as c:
        retry_job = dict(c.execute(select(jobs).where(jobs.c.id == JOB_ID)).mappings().one())
    worker.report_step(db, retry_job, renderer=lambda _result: pytest.fail("idempotent report was rerendered"))
    rows = report_rows(db)
    assert len(rows) == 1 and rows[0]["id"] == report_id and rows[0]["is_active"] is True


def test_test_only_report_is_preserved_and_superseded_by_genuine_docx(tmp_path):
    db, job, old_doc = setup_db(tmp_path, preview=True, other=True)
    worker.report_step(db, job, renderer=renderer)
    rows = report_rows(db)
    old = next(row for row in rows if row["id"] == "report-preview-TEST-ONLY")
    new = next(row for row in rows if row["result_id"] == RESULT_ID)
    assert old["document"] == old_doc
    assert old["result_id"] == PREVIEW_ID
    assert old["sha256"] == sha256(old_doc).hexdigest()
    assert old["is_active"] is False and old["superseded_by"] == new["id"]
    assert new["is_active"] is True and new["id"] != old["id"]
    assert new["document"] != old_doc and zipfile_is_docx(new["document"])
    with db.transaction() as c:
        review = dict(c.execute(select(report_reviews).where(report_reviews.c.id == "review-preview-approved")).mappings().one())
        assert review["report_id"] == old["id"] and review["document_sha256"] == old["sha256"]
        event = c.execute(select(events).where(events.c.event == "REPORT_VERSION_SUPERSEDED")).mappings().one()
        detail = json.loads(event["detail"])
        assert detail["old_sha256"] == old["sha256"] and detail["new_sha256"] == new["sha256"]
        other = dict(c.execute(select(reports).where(reports.c.id == "report-other")).mappings().one())
        assert other["job_id"] == "job-other" and other["is_active"] is True
        pending = c.scalar(select(func.count()).select_from(report_reviews).where(
            report_reviews.c.report_id == new["id"], report_reviews.c.document_sha256 == new["sha256"]))
        assert pending == 0
        current_job = dict(c.execute(select(jobs).where(jobs.c.id == JOB_ID)).mappings().one())
        assert current_job["result_id"] == RESULT_ID
        migration = c.scalar(select(func.max(schema_migrations.c.version)))
        assert migration == 9


def test_new_report_has_real_result_and_old_approval_cannot_pass_delivery_gate(tmp_path, monkeypatch):
    db, job, _ = setup_db(tmp_path, preview=True)
    worker.report_step(db, job, renderer=renderer)
    new = next(row for row in report_rows(db) if row["is_active"])
    assert new["result_id"] == RESULT_ID
    assert new["sha256"] != sha256(next(row["document"] for row in report_rows(db) if not row["is_active"])).hexdigest()
    monkeypatch.setenv("OUTREACH_REPORT_DELIVERY_ENABLED", "false")
    with db.transaction() as c:
        delivery_job = dict(c.execute(select(jobs).where(jobs.c.id == JOB_ID)).mappings().one())
    calls = []
    with pytest.raises(ConfigurationBlocked, match="awaits approval"):
        worker.delivery_step(db, delivery_job, lambda: calls.append("constructed"))
    assert calls == []


def test_concurrent_report_ticks_create_one_active_version(tmp_path):
    db, job, _ = setup_db(tmp_path, preview=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker.report_step(db, job, renderer=renderer), range(2)))
    rows = report_rows(db)
    assert len([row for row in rows if row["is_active"]]) == 1
    assert len([row for row in rows if row["result_id"] == RESULT_ID]) == 1
    assert all(result is None for result in results)


def test_report_delivery_disabled_even_for_freshly_approved_docx(tmp_path, monkeypatch):
    db, job, _ = setup_db(tmp_path)
    worker.report_step(db, job, renderer=renderer)
    report = next(row for row in report_rows(db) if row["is_active"])
    with db.transaction() as c:
        c.execute(admin_users.insert().values(id="admin-test", email="professor@example.edu", password_hash="unused",
            role="ADMIN", status="ACTIVE", created_at="2026-11-05T00:00:00+00:00"))
        c.execute(report_reviews.insert().values(id="review-new", report_id=report["id"],
            document_sha256=report["sha256"], decision="APPROVED", reviewer_email="professor@example.edu",
            reviewed_at="2026-11-05T00:02:00+00:00"))
        delivery_job = dict(c.execute(select(jobs).where(jobs.c.id == JOB_ID)).mappings().one())
    monkeypatch.setenv("OUTREACH_REPORT_DELIVERY_ENABLED", "false")
    calls = []
    with pytest.raises(ConfigurationBlocked, match="delivery is disabled"):
        worker.delivery_step(db, delivery_job, lambda: calls.append("constructed"))
    assert calls == []


def test_report_migration_is_versioned_and_partial_unique_index_is_present(tmp_path):
    db, _, _ = setup_db(tmp_path)
    with db.transaction() as c:
        indexes = c.exec_driver_sql("PRAGMA index_list('reports')").all()
        assert any(row[1] == "uq_reports_active_job" and row[2] == 1 for row in indexes)
        assert c.scalar(select(func.max(schema_migrations.c.version))) == 9


def test_migration_upgrades_legacy_unique_job_schema_and_preserves_review_fk(tmp_path):
    db, _, old_doc = setup_db(tmp_path, preview=True)
    with db.engine.connect() as c:
        c.exec_driver_sql("PRAGMA foreign_keys=OFF")
        c.commit()
        with c.begin():
            c.exec_driver_sql("DROP INDEX IF EXISTS uq_reports_active_job")
            c.exec_driver_sql('''CREATE TABLE reports_v8 (
                id VARCHAR NOT NULL PRIMARY KEY,job_id VARCHAR NOT NULL REFERENCES jobs(id),
                recipient VARCHAR NOT NULL,result_id VARCHAR UNIQUE,document BLOB,sha256 TEXT,created_at TEXT,
                approval_required BOOLEAN NOT NULL DEFAULT 1,UNIQUE(job_id))''')
            c.exec_driver_sql('''INSERT INTO reports_v8
                (id,job_id,recipient,result_id,document,sha256,created_at,approval_required)
                SELECT id,job_id,recipient,result_id,document,sha256,created_at,approval_required FROM reports''')
            c.exec_driver_sql("DROP TABLE reports")
            c.exec_driver_sql("ALTER TABLE reports_v8 RENAME TO reports")
            c.exec_driver_sql("DELETE FROM schema_migrations WHERE version=9")
    db.initialize()
    with db.transaction() as c:
        row=c.execute(select(reports).where(reports.c.id=="report-preview-TEST-ONLY")).mappings().one()
        assert row["document"]==old_doc and row["result_id"]==PREVIEW_ID
        review=c.execute(select(report_reviews).where(report_reviews.c.id=="review-preview-approved")).mappings().one()
        assert review["report_id"]==row["id"] and review["document_sha256"]==row["sha256"]
        uniques=c.exec_driver_sql("PRAGMA index_list('reports')").all()
        assert any(index[1]=="uq_reports_active_job" and index[2]==1 for index in uniques)
        inactive_doc=make_docx("archived")
        c.execute(reports.insert().values(id="report-v9-inactive",job_id=JOB_ID,recipient="owner@example.test",
            result_id="second-result",document=inactive_doc,sha256=sha256(inactive_doc).hexdigest(),
            created_at="2026-11-05T00:00:00+00:00",is_active=False))
        assert not c.exec_driver_sql("PRAGMA foreign_key_check").all()


def zipfile_is_docx(data):
    import zipfile
    return zipfile.is_zipfile(BytesIO(data))
