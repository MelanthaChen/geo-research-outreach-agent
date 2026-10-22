import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from migrate_candidate_sources import migrate
from outreach_agent.dashboard_data import cloud_dashboard_snapshot


def _source_databases(tmp_path):
    business = tmp_path / "business.db"
    hotel = tmp_path / "hotel.db"
    with sqlite3.connect(business) as db:
        db.executescript("""
        CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, company_name TEXT, website TEXT, canonical_domain TEXT, industry TEXT, location TEXT, source_name TEXT, source_url TEXT, source_record_id TEXT, source_license_or_permission TEXT, discovery_timestamp TEXT, verification_status TEXT, geo_eligibility_status TEXT, geo_opportunity_status TEXT, contact_status TEXT, human_review_status TEXT, outreach_ready_status TEXT, verification_reason TEXT, qualification_evidence TEXT, fetch_status TEXT, http_status TEXT, evidence_json TEXT, last_inspected_at TEXT, source_batch TEXT);
        CREATE TABLE candidate_provenance (candidate_id TEXT, source_name TEXT, source_url TEXT, source_record_id TEXT, source_license_or_permission TEXT, discovery_timestamp TEXT, PRIMARY KEY(candidate_id, source_name, source_record_id));
        CREATE TABLE phase6f_contact_channels (candidate_id TEXT, channel_type TEXT, email TEXT, channel_url TEXT, source_url TEXT, evidence TEXT, purpose TEXT, review_status TEXT, PRIMARY KEY(candidate_id, channel_type, channel_url));
        CREATE TABLE phase6e_screening (candidate_id TEXT PRIMARY KEY, screen_status TEXT, priority_score TEXT, category_group TEXT, chain_location_concern TEXT, rule_version TEXT, screened_at TEXT, manual_sample_class TEXT, manual_sample_note TEXT, website_domain_concern TEXT, priority_basis TEXT);
        """)
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("c1", "Example Co", "https://example.test", "example.test", "software", "Boston, MA, United States", "fixture", "https://source.test/c1", "r1", "test", "2026-01-01", "VERIFIED", "ELIGIBLE", "HIGH", "EMAIL", "PENDING", "READY", "directory", "evidence", "DONE", "200", "{}", "2026-01-01", "batch-1"))
        db.execute("INSERT INTO candidate_provenance VALUES (?,?,?,?,?,?)", ("c1", "fixture", "https://source.test/c1", "r1", "test", "2026-01-01"))
        db.execute("INSERT INTO phase6f_contact_channels VALUES (?,?,?,?,?,?,?,?)", ("c1", "BUSINESS_EMAIL", "owner@example.test", "", "https://example.test/contact", "public", "research", "PENDING"))
        db.execute("INSERT INTO phase6e_screening VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("c1", "SCREENED", "9", "software", "", "v1", "2026-01-01", "", "", "", "evidence"))
    with sqlite3.connect(hotel) as db:
        db.executescript("""
        CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, company_name TEXT, website TEXT, canonical_domain TEXT, industry TEXT, location TEXT, source_name TEXT, source_url TEXT, source_record_id TEXT, source_license_or_permission TEXT, discovery_timestamp TEXT, verification_status TEXT, geo_eligibility_status TEXT, geo_opportunity_status TEXT, contact_status TEXT, human_review_status TEXT, outreach_ready_status TEXT, verification_reason TEXT, qualification_evidence TEXT, fetch_status TEXT, http_status TEXT, evidence_json TEXT, last_inspected_at TEXT, source_batch TEXT, property_type TEXT, address TEXT, chain_name TEXT, chain_status TEXT, amenities_json TEXT, room_evidence_json TEXT, guest_segments_json TEXT, operating_evidence_status TEXT);
        CREATE TABLE hotel_provenance (candidate_id TEXT, source_record_id TEXT, source_file TEXT, source_name TEXT, source_url TEXT, source_license_or_permission TEXT, source_dataset_url TEXT, discovery_timestamp TEXT, PRIMARY KEY(candidate_id, source_record_id, source_file));
        CREATE TABLE hotel_contact_channels (candidate_id TEXT, channel_type TEXT, email TEXT, channel_url TEXT, source_url TEXT, evidence TEXT, purpose TEXT, review_status TEXT, discovered_at TEXT, PRIMARY KEY(candidate_id, channel_type, email, channel_url));
        """)
        db.execute("INSERT INTO candidates VALUES (" + ",".join("?" for _ in range(32)) + ")", ("h1", "Fixture Hotel", "https://hotel.test", "hotel.test", "hotel", "Chicago, IL, United States", "fixture", "https://source.test/h1", "h1", "test", "2026-01-01", "VERIFIED", "ELIGIBLE", "MEDIUM", "FORM", "PENDING", "NOT_READY", "directory", "evidence", "DONE", "200", "{}", "2026-01-01", "batch-1", "hotel", "Chicago, IL", "Example Chain", "CHAIN", "{}", "{}", "{}", "VERIFIED"))
        db.execute("INSERT INTO hotel_provenance VALUES (?,?,?,?,?,?,?,?)", ("h1", "h1", "hotels.csv", "fixture", "https://source.test/h1", "test", "https://source.test", "2026-01-01"))
        db.execute("INSERT INTO hotel_contact_channels VALUES (?,?,?,?,?,?,?,?,?)", ("h1", "CONTACT_FORM", "", "https://hotel.test/contact", "https://hotel.test/contact", "public", "research", "PENDING", "2026-01-01"))
    return business, hotel


def test_candidate_source_migration_is_idempotent_and_reconciled(tmp_path):
    business, hotel = _source_databases(tmp_path)
    target = tmp_path / "registry.db"
    first = migrate(business_db=business, hotel_db=hotel, registry_db=target, report_path=tmp_path / "first.json")
    second = migrate(business_db=business, hotel_db=hotel, registry_db=target, report_path=tmp_path / "second.json")
    assert first["counts"] == {"company": 1, "hotel": 1}
    assert first["reconciliation"]["company"]["target_rows"] == 1
    assert first["reconciliation"]["hotel"]["target_rows"] == 1
    assert second["reconciliation"]["company"]["target_rows"] == 1
    assert second["reconciliation"]["hotel"]["target_rows"] == 1
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM candidate_companies").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM candidate_hotels").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM candidate_company_contacts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM candidate_hotel_contacts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='business_company'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='hotel_property'").fetchone()[0] == 1
    assert json.loads((tmp_path / "first.json").read_text())["schema_version"] == "candidate-source-reconciliation/v1"


def test_dry_run_does_not_modify_registry(tmp_path):
    business, hotel = _source_databases(tmp_path)
    target = tmp_path / "registry.db"
    with sqlite3.connect(target) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('unchanged')")
    migrate(business_db=business, hotel_db=hotel, registry_db=target, dry_run=True)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='candidate_companies'").fetchone() is None


def test_cloud_dashboard_snapshot_reads_migrated_registry(tmp_path):
    business, hotel = _source_databases(tmp_path)
    target = tmp_path / "registry.db"
    migrate(business_db=business, hotel_db=hotel, registry_db=target)
    snapshot = cloud_dashboard_snapshot(target)
    assert snapshot["summary"]["cloud_registry"] is True
    assert snapshot["summary"]["total_companies"] == 2
    assert snapshot["companies"] == []
