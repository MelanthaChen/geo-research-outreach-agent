import json
import sqlite3

import outreach_agent.hotel_prospect_job as hotel_job
from outreach_agent.hotel_prospect_job import discover_nominatim_source, discover_overpass_source, pause, run_once, status
from outreach_agent.lean_registry import init_lean_registry


def _source(path):
    rows = [
        {
            "source_record_id": "a", "property_name": "Harbor Hotel", "address": "1 Main St",
            "location": "Boston, MA, United States", "official_website": "https://harbor.example",
            "verification_status": "VERIFIED", "qualification_status": "ELIGIBLE",
            "source_name": "public tourism directory", "source_url": "https://directory.example/a",
            "contact_channels": [{"channel_type": "CONTACT_FORM", "channel_url": "https://harbor.example/contact", "source_url": "https://harbor.example/contact", "evidence": "Contact form observed"}],
        },
        {
            "source_record_id": "b", "property_name": "Harbor Hotel - Downtown", "address": "2 Main St",
            "location": "Boston, MA, United States", "official_website": "https://chain.example",
            "verification_status": "VERIFIED", "qualification_status": "ELIGIBLE",
            "source_name": "public tourism directory", "source_url": "https://directory.example/b",
            "contact_channels": [{"channel_type": "GENERAL_BUSINESS_EMAIL", "email": "desk@chain.example", "source_url": "https://chain.example/contact", "evidence": "Public business email observed"}],
        },
        {
            "source_record_id": "c", "property_name": "Foreign Hotel", "address": "1 High St",
            "location": "London, United Kingdom", "official_website": "https://foreign.example",
            "verification_status": "VERIFIED", "qualification_status": "ELIGIBLE", "source_name": "directory",
            "source_url": "https://directory.example/c", "contact_channels": [{"channel_type": "CONTACT_FORM", "channel_url": "https://foreign.example/contact"}],
        },
        {
            "source_record_id": "d", "property_name": "Unverified Hotel", "address": "3 Main St",
            "location": "Austin, TX, United States", "official_website": "https://unknown.example",
            "verification_status": "DISCOVERED_UNVERIFIED", "qualification_status": "ELIGIBLE", "source_name": "directory",
            "source_url": "https://directory.example/d", "contact_channels": [{"channel_type": "CONTACT_FORM", "channel_url": "https://unknown.example/contact"}],
        },
    ]
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_hotel_job_preserves_property_identity_and_does_not_verify_email(tmp_path):
    source, registry = tmp_path / "source.json", tmp_path / "registry.db"
    _source(source)
    result = run_once(registry, source, target_count=2, batch_size=4)
    assert result["promoted"] == 2
    assert result["rejected"] == 0  # the job stops exactly at the requested target
    with sqlite3.connect(registry) as db:
        rows = db.execute("SELECT name,official_website,promotion_state,contact_verification_status FROM research_records WHERE profile_type='hotel_property' ORDER BY name").fetchall()
        assert len(rows) == 2
        assert {row[2:] for row in rows} == {("PROSPECT", "CONTACT_REVIEW_PENDING")}
        assert db.execute("SELECT COUNT(*) FROM prospect_contacts WHERE email='desk@chain.example' AND email_verification_status='NOT_VERIFIED'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM prospect_contacts WHERE channel_type='CONTACT_FORM'").fetchone()[0] == 1


def test_hotel_job_is_idempotent_and_chain_domains_do_not_merge_properties(tmp_path):
    source, registry = tmp_path / "source.json", tmp_path / "registry.db"
    _source(source)
    first = run_once(registry, source, target_count=10, batch_size=2)
    second = run_once(registry, source, target_count=10, batch_size=2)
    assert first["promoted"] == 2
    assert second["promoted"] == 0
    with sqlite3.connect(registry) as db:
        assert db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='hotel_property'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM hotel_discovery_events WHERE event_type='SEEN'").fetchone()[0] == 4


def test_hotel_job_pause_resume_and_exact_target(tmp_path):
    source, registry = tmp_path / "source.json", tmp_path / "registry.db"
    _source(source)
    result = run_once(registry, source, target_count=1, batch_size=4)
    assert result["processed"] == 1
    assert result["promoted"] == 1
    assert status(registry)["status"] == "TARGET_REACHED"
    pause(registry)
    assert status(registry)["status"] == "PAUSED"


def test_hotel_job_blocks_suppressed_and_withdrawn_rows(tmp_path):
    source, registry = tmp_path / "source.json", tmp_path / "registry.db"
    rows = [{
        "source_record_id": "suppressed", "property_name": "Suppressed Hotel", "address": "1 Main St",
        "location": "Austin, TX, United States", "official_website": "https://suppressed.example",
        "verification_status": "VERIFIED", "qualification_status": "ELIGIBLE", "source_name": "directory",
        "contact_channels": [{"channel_type": "GENERAL_BUSINESS_EMAIL", "email": "blocked@suppressed.example", "source_url": "https://suppressed.example/contact"}],
    }, {
        "source_record_id": "withdrawn", "property_name": "Withdrawn Hotel", "address": "2 Main St",
        "location": "Austin, TX, United States", "official_website": "https://withdrawn.example",
        "verification_status": "VERIFIED", "qualification_status": "ELIGIBLE", "source_name": "directory",
        "withdrawal_status": "WITHDRAWN", "contact_channels": [{"channel_type": "CONTACT_FORM", "channel_url": "https://withdrawn.example/contact"}],
    }]
    source.write_text(json.dumps(rows), encoding="utf-8")
    init_lean_registry(registry)
    with sqlite3.connect(registry) as db:
        db.execute("INSERT INTO suppressed_recipients VALUES(?,?,datetime('now'))", ("blocked@suppressed.example", "operator suppression"))
    result = run_once(registry, source, target_count=10, batch_size=2)
    assert result["promoted"] == 0
    assert result["rejected"] == 2


def test_overpass_adapter_preserves_property_identity_and_contact_evidence():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"elements": [{
                "type": "node", "id": 123,
                "tags": {
                    "name": "Independent Harbor Inn",
                    "addr:housenumber": "1", "addr:street": "Main St",
                    "addr:city": "Boston", "addr:state": "MA",
                    "addr:postcode": "02108",
                    "contact:website": "harbor.example",
                    "contact:email": "stay@harbor.example",
                },
            }]}

    class Client:
        def post(self, *args, **kwargs):
            return Response()

    rows, meta, next_cursor = discover_overpass_source(0, client=Client(), max_elements=10)
    assert next_cursor == 1
    assert meta["region"] == "boston"
    assert rows[0]["source_record_id"] == "node/123"
    assert rows[0]["official_website"] == "https://harbor.example"
    assert rows[0]["location"] == "Boston, MA, United States"
    assert rows[0]["contact_channels"][0]["channel_type"] == "GENERAL_BUSINESS_EMAIL"


def test_nominatim_fallback_preserves_us_location_and_is_bounded():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{
                "osm_type": "way", "osm_id": 456, "name": "Fallback Hotel",
                "display_name": "Fallback Hotel, 1 Main St, Boston, Massachusetts, United States",
                "address": {"city": "Boston", "state": "Massachusetts", "country": "United States"},
                "extratags": {"rooms": "20"},
            }]

    class Client:
        def get(self, url, params):
            assert params["limit"] <= 25
            assert params["q"] == "hotel boston USA"
            return Response()

    rows, meta, next_cursor = discover_nominatim_source(0, client=Client(), max_elements=100)
    assert next_cursor == 1
    assert meta["region"] == "boston"
    assert rows[0]["source_record_id"] == "way/456"
    assert rows[0]["location"].endswith("United States")
    assert rows[0]["official_website"] == ""


def test_overpass_mode_uses_existing_pipeline_and_tracks_source_metrics(tmp_path, monkeypatch):
    registry = tmp_path / "registry.db"
    row = {
        "source_record_id": "node/900", "candidate_id": "overpass-hotel:900",
        "property_name": "Owner Controlled Test Inn", "address": "9 Main St",
        "location": "Austin, TX, United States", "official_website": "https://inn.example",
        "verification_status": "VERIFIED", "qualification_status": "ELIGIBLE",
        "source_name": "OpenStreetMap Overpass US region boston",
        "source_url": "https://overpass-api.de/api/interpreter",
        "source_license_or_permission": "OpenStreetMap ODbL 1.0",
        "source_batch": "boston", "contact_channels": [{
            "channel_type": "CONTACT_FORM", "channel_url": "https://inn.example/contact",
            "source_url": "https://inn.example/contact", "evidence": "Observed public form",
        }],
    }

    def fake_source(cursor, **kwargs):
        return [row], {"source_name": "test-overpass", "region": "test"}, cursor + 1

    monkeypatch.setattr(hotel_job, "discover_overpass_source", fake_source)
    monkeypatch.setattr(hotel_job, "_verify_source_rows", lambda rows, request_budget: rows)
    first = run_once(registry, source_mode="OVERPASS", target_count=10, batch_size=1)
    second = run_once(registry, source_mode="OVERPASS", target_count=10, batch_size=1)
    assert first["promoted"] == 1
    assert second["duplicates"] == 1
    with sqlite3.connect(registry) as db:
        stats = db.execute("SELECT rows_discovered,rows_verified,rows_promoted,rows_duplicate FROM hotel_discovery_source_stats WHERE source_name='test-overpass'").fetchone()
        assert stats == (2, 2, 1, 1)
        assert db.execute("SELECT COUNT(*) FROM research_records WHERE profile_type='hotel_property'").fetchone()[0] == 1
