import sqlite3

from outreach_agent.phase7 import (
    _confirmed_form_evidence,
    _cumulative_elapsed_seconds,
    _exclusive_run_lock,
    _has_operational_evidence,
    _suitable_email,
    select_remaining,
    _union_run_wall_seconds,
)


def test_operational_evidence_requires_success_and_substantive_content():
    assert _has_operational_evidence('{"saved_snapshot":{"fetch_status":"SUCCESS","http_status":200,"visible_text_length":100}}')
    assert not _has_operational_evidence('{"pages":[{"fetch_status":"SUCCESS","http_status":200,"visible_text_length":20}]}')
    assert not _has_operational_evidence('{"pages":[{"fetch_status":"FAILED","http_status":200,"visible_text_length":500}]}')


def test_contact_suitability_excludes_inappropriate_role_inboxes():
    assert _suitable_email("research@company.example")
    assert _suitable_email("founders@company.example")
    assert not _suitable_email("support@company.example")
    assert not _suitable_email("privacy@company.example")
    assert not _suitable_email("noreply-hello@company.example")


def test_form_requires_identifiable_controls_not_contact_page_text():
    assert _confirmed_form_evidence("Name email textarea Send message")
    assert _confirmed_form_evidence("form id=contact <button>Submit</button>")
    assert not _confirmed_form_evidence("Contact us to learn more; send us a message")


def test_cumulative_time_cap_adds_prior_runs_and_current_wall_time_once():
    assert _cumulative_elapsed_seconds(100.0, 25.0) == 125.0
    assert _cumulative_elapsed_seconds(-5.0, 25.0) == 25.0


def test_run_wall_time_unions_overlapping_intervals_once():
    intervals = [
        ("2026-10-15T00:00:00+00:00", "2026-10-15T00:00:10+00:00"),
        ("2026-10-15T00:00:05+00:00", "2026-10-15T00:00:15+00:00"),
        ("2026-10-15T00:00:20+00:00", "2026-10-15T00:00:22+00:00"),
    ]
    assert _union_run_wall_seconds(intervals) == 17


def test_run_lock_rejects_overlapping_resume_for_same_database(tmp_path):
    db_path = tmp_path / "phase7.sqlite"
    with _exclusive_run_lock(db_path):
        try:
            with _exclusive_run_lock(db_path):
                assert False, "second run lock should fail"
        except RuntimeError as exc:
            assert "already active" in str(exc)


def test_selection_is_full_pool_deterministic_and_resumable(tmp_path):
    db_path = tmp_path / "phase7.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE candidates(candidate_id TEXT PRIMARY KEY,canonical_domain TEXT,verification_status TEXT)")
        db.execute("""CREATE TABLE phase6e_screening(candidate_id TEXT PRIMARY KEY,screen_status TEXT,
          priority_score INTEGER,chain_location_concern TEXT,website_domain_concern TEXT,category_group TEXT)""")
        candidates = [
            ("uncertain", "b.example", "DISCOVERED_UNVERIFIED", "UNCERTAIN", 999, "NO_STRUCTURED_SIGNAL", "NO_OBVIOUS_PATTERN", "x"),
            ("likely", "z.example", "DISCOVERED_UNVERIFIED", "LIKELY_FIT", 1, "NO_STRUCTURED_SIGNAL", "NO_OBVIOUS_PATTERN", "x"),
            ("done", "a.example", "VERIFIED", "LIKELY_FIT", 1000, "NO_STRUCTURED_SIGNAL", "NO_OBVIOUS_PATTERN", "x"),
            ("chain", "c.example", "DISCOVERED_UNVERIFIED", "LIKELY_FIT", 9, "LIKELY_CHAIN_OR_FRANCHISE", "NO_OBVIOUS_PATTERN", "x"),
        ]
        for cid, domain, status, fit, score, chain, website, category in candidates:
            db.execute("INSERT INTO candidates VALUES(?,?,?)", (cid, domain, status))
            db.execute("INSERT INTO phase6e_screening VALUES(?,?,?,?,?,?)", (cid, fit, score, chain, website, category))
        db.commit()

    assert select_remaining(db_path=db_path) == 3
    with sqlite3.connect(db_path) as db:
        ordered = [row[0] for row in db.execute("SELECT candidate_id FROM phase7_selection ORDER BY selection_rank")]
        db.execute("UPDATE candidates SET verification_status='VERIFIED' WHERE candidate_id='likely'")
        db.commit()
    assert ordered == ["likely", "chain", "uncertain"]
    assert select_remaining(db_path=db_path) == 3
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM phase7_selection").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM phase7_selection WHERE candidate_id='done'").fetchone()[0] == 0
