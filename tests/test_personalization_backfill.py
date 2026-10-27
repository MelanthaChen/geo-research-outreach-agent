import json
import sqlite3

from outreach_agent.personalization_backfill import create_job, eligible_profile_ids, run_job
from outreach_agent.research_lifecycle import register_profile


def profile(profile_id="company-backfill-1", *, owner=False, qualification="ELIGIBLE"):
    return {
        "schema_version": "geo-company-profile/v1", "profile_id": profile_id,
        "profile_type": "owner_controlled_test" if owner else "startup_company",
        "identity": {"name": "Cedar Studio", "website": "https://cedar.example", "verification_status": "VERIFIED", "verification_evidence": [{"url": "https://cedar.example"}]},
        "startup": {"industry": "design services", "location": "Austin, TX"},
        "qualification": {"eligibility": qualification},
        "provenance": {"source_name": "test", "source_batch": "test"},
        "contact_channels": [{"channel_type": "EMAIL_ONLY", "email": "hello@cedar.example", "source_url": "https://cedar.example/contact"}],
    }


def fetcher(*args, **kwargs):
    return ([{"url": "https://cedar.example/", "title": "Cedar Studio", "text": "Cedar Studio in Austin offers design services."}], {"robots": "ALLOWED", "mode": "TEST", "http_status": "200"})


def test_isolated_backfill_persists_profile_and_is_idempotent(tmp_path):
    path = tmp_path / "registry.db"
    register_profile(profile(), path)
    register_profile(profile("owner-1", owner=True), path)
    assert eligible_profile_ids(path) == ["company-backfill-1"]
    job = create_job(path)
    assert run_job(job, path, fetcher=fetcher) == {"job_id": job, "status": "COMPLETE", "processed": 1, "remaining": 0, "succeeded": 1, "failed": 0}
    with sqlite3.connect(path) as db:
        row = db.execute("select personalization_status, personalization_json from research_records where profile_id='company-backfill-1'").fetchone()
        assert row[0] == "COMPLETE"
        payload = json.loads(row[1]); assert payload["data_classification"] == "OBSERVED"
        assert db.execute("select count(*) from screening_analysis_results").fetchone()[0] == 1
        assert db.execute("select count(*) from research_records where profile_type='owner_controlled_test'").fetchone()[0] == 1
    second = run_job(job, path, fetcher=lambda *a, **k: (_ for _ in ()).throw(AssertionError("completed work must not refetch")))
    assert second["processed"] == 0 and second["remaining"] == 0
