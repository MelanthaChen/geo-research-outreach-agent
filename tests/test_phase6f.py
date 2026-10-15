import csv
import json
import sqlite3
import pytest

from outreach_agent.candidate_pool import init_candidate_db
from outreach_agent.phase6f import (
    _extract_verified_channels, dashboard_overlays, dashboard_summary, export_outputs, initialize,
    run_pilot, select_pilot,
)


def _candidate(db, cid, domain, *, status="DISCOVERED_UNVERIFIED", industry="shop=clothes"):
    db.execute("""INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,geo_eligibility_status,geo_opportunity_status,contact_status,human_review_status,outreach_ready_status,source_batch)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (cid, f"Company {cid[-2:]}", f"https://{domain}", domain, industry, "US", "OSM", "https://source.invalid", cid, "ODbL", "2026-01-01", status, "UNKNOWN", "UNKNOWN", "NOT_DISCOVERED", "PENDING", "NOT_READY", "osm"))


def test_selection_is_deterministic_and_excludes_previously_attempted(tmp_path):
    db_path = tmp_path / "pool.db"
    init_candidate_db(db_path)
    initialize(db_path, db_path)
    with sqlite3.connect(db_path) as db:
        _candidate(db, "osm:a", "a.example.com")
        _candidate(db, "osm:b", "b.example.com", status="AMBIGUOUS_IDENTITY")
        _candidate(db, "osm:c", "c.example.com")
    shortlist = tmp_path / "top.csv"
    fields = ["candidate_id", "screen_status", "priority_score", "chain_location_concern", "website_domain_concern"]
    with shortlist.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows([
            {"candidate_id": "osm:a", "screen_status": "LIKELY_FIT", "priority_score": "90", "chain_location_concern": "NO_STRUCTURED_SIGNAL", "website_domain_concern": "NO_OBVIOUS_PATTERN"},
            {"candidate_id": "osm:b", "screen_status": "LIKELY_FIT", "priority_score": "100", "chain_location_concern": "NO_STRUCTURED_SIGNAL", "website_domain_concern": "NO_OBVIOUS_PATTERN"},
            {"candidate_id": "osm:c", "screen_status": "UNCERTAIN", "priority_score": "50", "chain_location_concern": "NO_STRUCTURED_SIGNAL", "website_domain_concern": "NO_OBVIOUS_PATTERN"},
        ])
    assert [r["candidate_id"] for r in select_pilot(2, shortlist=shortlist, db_path=db_path)] == ["osm:a", "osm:c"]
    assert [r["candidate_id"] for r in select_pilot(2, shortlist=shortlist, db_path=db_path)] == ["osm:a", "osm:c"]


def test_contact_extraction_requires_verified_geo_eligible_and_first_party(tmp_path):
    db_path = tmp_path / "pool.db"
    init_candidate_db(db_path)
    initialize(db_path, db_path)
    with sqlite3.connect(db_path) as db:
        _candidate(db, "osm:a", "a.example.com", status="VERIFIED")
        db.execute("UPDATE candidates SET geo_eligibility_status='ELIGIBLE', evidence_json=? WHERE candidate_id='osm:a'", (json.dumps({"pages": [{"final_url": "https://a.example.com/contact", "extracted_channels": json.dumps([
            {"type": "GENERAL_BUSINESS_EMAIL", "email": "info@a.example.com", "url": "https://a.example.com/contact", "source_url": "https://a.example.com/contact", "evidence": "Contact us"},
            {"type": "CONTACT_FORM", "email": None, "url": "https://third-party.example/form", "source_url": "https://a.example.com/contact", "evidence": "Contact form"},
        ])}]}),))
        db.execute("INSERT INTO phase6f_selection(candidate_id,selection_rank,selection_reason,selected_from,selected_at) VALUES('osm:a',1,'rule','fixture','now')")
        db.commit()
    assert _extract_verified_channels(db_path) == 2
    summary = dashboard_summary(db_path)
    assert summary["contact_channels_found"] == 2
    assert dashboard_overlays(["osm:a"], db_path)["osm:a"]["contact_status"] == "CONTACT_FOUND"
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT email,channel_url,review_status FROM phase6f_contact_channels WHERE email IS NOT NULL").fetchone()
    assert row == ("info@a.example.com", "https://a.example.com/contact", "PENDING")
    assert export_outputs(db_path=db_path, output_dir=tmp_path)["phase6f_contact_candidates.csv"] == 2


def test_export_is_stable_and_pending_is_not_failure(tmp_path):
    db_path = tmp_path / "pool.db"
    init_candidate_db(db_path)
    initialize(db_path, db_path)
    with sqlite3.connect(db_path) as db:
        _candidate(db, "osm:a", "a.example.com")
        db.execute("INSERT INTO phase6f_selection(candidate_id,selection_rank,selection_reason,selected_from,selected_at) VALUES('osm:a',1,'rule','fixture','now')")
        db.commit()
    export_outputs(db_path=db_path, output_dir=tmp_path)
    path = tmp_path / "phase6f_validation_failures.csv"
    first = path.read_bytes()
    export_outputs(db_path=db_path, output_dir=tmp_path)
    assert path.read_bytes() == first
    assert dashboard_summary(db_path)["pending_verification"] == 1
    with path.open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == []


def test_paid_service_cost_cap_is_zero(tmp_path):
    with pytest.raises(ValueError, match=r"strict \$0.00"):
        run_pilot(cost_cap_usd=0.01, db_path=tmp_path / "not-created.db")
    assert not (tmp_path / "not-created.db").exists()
