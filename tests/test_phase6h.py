import csv
import json
import sqlite3

from outreach_agent.candidate_pool import init_candidate_db
from outreach_agent.phase6f import initialize as initialize_phase6f
from outreach_agent.phase6h import (
    _extract_channels, dashboard_summary, execution_metrics, export_outputs, initialize, reconcile,
    run, select_remaining,
)


def _candidate(db, cid, domain, *, status="DISCOVERED_UNVERIFIED"):
    db.execute("""INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,
      source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,
      geo_eligibility_status,geo_opportunity_status,contact_status,source_batch)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (cid, cid, f"https://{domain}", domain, "shop=books", "US", "OSM", f"https://osm.org/{cid}", cid,
       "ODbL", "now", status, "UNKNOWN", "UNKNOWN", "NOT_DISCOVERED", "fixture"))


def _fixture(tmp_path):
    phase6e = tmp_path / "phase6e.db"
    init_candidate_db(phase6e)
    with sqlite3.connect(phase6e) as db:
        for letter in "abcd":
            _candidate(db, f"osm:{letter}", f"{letter}.example", status="VERIFIED" if letter == "d" else "DISCOVERED_UNVERIFIED")
        db.commit()
    phase6f = tmp_path / "phase6f.db"
    initialize_phase6f(phase6f, phase6e)
    with sqlite3.connect(phase6f) as db:
        db.execute("INSERT INTO phase6f_selection VALUES(?,?,?,?,?)", ("osm:a", 1, "prior", "fixture", "now"))
        db.execute("UPDATE candidates SET verification_status='FETCH_FAILED' WHERE candidate_id='osm:a'")
        db.commit()
    shortlist = tmp_path / "top.csv"
    with shortlist.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["candidate_id", "screen_status", "chain_location_concern", "website_domain_concern", "priority_score"])
        writer.writeheader()
        for cid in ("osm:a", "osm:b", "osm:c", "osm:d"):
            writer.writerow({"candidate_id": cid, "screen_status": "LIKELY_FIT", "chain_location_concern": "NO_STRUCTURED_SIGNAL", "website_domain_concern": "NO_OBVIOUS_PATTERN", "priority_score": 80})
    return phase6e, phase6f, shortlist


def test_reconcile_excludes_phase6f_attempts_and_terminal_phase6e(tmp_path):
    phase6e, phase6f, shortlist = _fixture(tmp_path)
    result = reconcile(shortlist, phase6e, phase6f)
    assert result["shortlist_unique_ids"] == 4
    assert result["phase6e_terminal_before_phase6f"] == 1
    assert result["phase6f_selected_in_shortlist"] == 1
    assert result["remaining_first_pass_workload"] == 2


def test_selection_is_stable_and_does_not_repeat_prior_attempts(tmp_path):
    phase6e, phase6f, shortlist = _fixture(tmp_path)
    h_db = tmp_path / "phase6h.db"
    initialize(h_db, phase6f)
    assert select_remaining(shortlist=shortlist, phase6e_db=phase6e, phase6f_db=phase6f, db_path=h_db) == 2
    with sqlite3.connect(h_db) as db:
        assert [r[0] for r in db.execute("SELECT candidate_id FROM phase6h_selection ORDER BY selection_rank")] == ["osm:b", "osm:c"]
        assert db.execute("SELECT COUNT(*) FROM phase6h_shortlist").fetchone()[0] == 4


def test_contact_extraction_is_same_domain_and_pending(tmp_path):
    phase6e, phase6f, shortlist = _fixture(tmp_path)
    h_db = tmp_path / "phase6h.db"
    initialize(h_db, phase6f)
    select_remaining(shortlist=shortlist, phase6e_db=phase6e, phase6f_db=phase6f, db_path=h_db)
    evidence = {"pages": [{"final_url": "https://b.example/contact", "extracted_channels": json.dumps([
        {"type": "GENERAL_BUSINESS_EMAIL", "email": "hello@b.example", "url": "https://b.example/contact", "source_url": "https://b.example/contact", "purpose": "business inquiry"},
        {"type": "CONTACT_FORM", "url": "https://external.example/form", "source_url": "https://b.example/contact", "evidence": "textarea submit"},
    ])}]}
    with sqlite3.connect(h_db) as db:
        db.execute("UPDATE candidates SET verification_status='VERIFIED',geo_eligibility_status='ELIGIBLE',evidence_json=? WHERE candidate_id='osm:b'", (json.dumps(evidence),))
        db.commit()
    assert _extract_channels(h_db) == 2
    with sqlite3.connect(h_db) as db:
        rows = db.execute("SELECT channel_type,email,review_status FROM phase6f_contact_channels WHERE candidate_id='osm:b'").fetchall()
        assert ("GENERAL_BUSINESS_EMAIL", "hello@b.example", "PENDING") in rows
        assert ("CONTACT_PAGE", None, "PENDING") in rows
        assert not any(row[0] == "CONTACT_FORM" for row in rows)


def test_dashboard_distinguishes_shortlist_and_processed_workload(tmp_path):
    phase6e, phase6f, shortlist = _fixture(tmp_path)
    h_db = tmp_path / "phase6h.db"
    initialize(h_db, phase6f)
    select_remaining(shortlist=shortlist, phase6e_db=phase6e, phase6f_db=phase6f, db_path=h_db)
    summary = dashboard_summary(h_db, shortlist, phase6e_db=phase6e, phase6f_db=phase6f)
    assert summary["shortlist"] == 4
    assert summary["starting_workload"] == 2
    assert summary["remaining"] == 2
    assert summary["outreach_ready"] == 0


def test_run_resumes_from_saved_status_and_exports_deterministically(tmp_path, monkeypatch):
    import outreach_agent.phase6h as phase6h

    phase6e, phase6f, shortlist = _fixture(tmp_path)
    h_db = tmp_path / "phase6h.db"
    output_dir = tmp_path / "exports"
    review_db = tmp_path / "review.db"
    calls = []

    def fake_verify(limit, *, candidate_db, candidate_ids, **kwargs):
        calls.extend(candidate_ids)
        with sqlite3.connect(candidate_db) as db:
            for cid in candidate_ids:
                db.execute("UPDATE candidates SET verification_status='VERIFIED',geo_eligibility_status='ELIGIBLE',geo_opportunity_status='HIGH' WHERE candidate_id=?", (cid,))
            db.commit()
        return {"attempted": len(candidate_ids), "remaining": 0, "runtime_seconds": 0.1,
                "requests_attempted": len(candidate_ids), "requests_succeeded": len(candidate_ids),
                "requests_failed": 0, "retries": 0, "request_budget_exhausted": False,
                "time_budget_exhausted": False, "counts": {"VERIFIED": len(candidate_ids)}}

    monkeypatch.setattr(phase6h, "verify_candidate_batch", fake_verify)
    result = run(db_path=h_db, shortlist=shortlist, batch_size=2, request_cap=10, time_cap_seconds=10,
                 output_dir=output_dir, review_path=review_db, phase6e_db=phase6e, phase6f_db=phase6f)
    assert result["processed_new"] == 2
    assert result["processed_this_invocation"] == 2
    assert result["pending_verification"] == 0
    assert calls == ["osm:b", "osm:c"], result
    metrics = execution_metrics(h_db, review_path=review_db)
    assert metrics[0]["cohort_batch_range"] == "1"
    assert metrics[0]["processed"] == 2
    assert metrics[0]["identity_verification_rate"] == 1.0
    assert metrics[0]["geo_eligibility_rate"] == 1.0
    first = (output_dir / "phase6h_verified_companies.csv").read_bytes()
    export_outputs(db_path=h_db, output_dir=output_dir, review_path=review_db)
    assert (output_dir / "phase6h_verified_companies.csv").read_bytes() == first
    with sqlite3.connect(h_db) as db:
        assert db.execute("SELECT stop_reason FROM phase6h_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "selection_complete"
