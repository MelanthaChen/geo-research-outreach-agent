import sqlite3
import json

from outreach_agent.hotels import (export_geo_platform_profiles, export_startup_geo_profiles, hotel_batch_quality_pause,
    export_hotel_cohort_membership, export_hotels, hotel_summary, init_hotel_db, select_hotel_pilot)


def test_hotel_layer_keeps_distinct_chain_properties_on_same_domain(tmp_path):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    with sqlite3.connect(database) as db:
        for property_id, name in (("osm-hotel:node/1", "Chain Hotel Downtown"), ("osm-hotel:node/2", "Chain Hotel Airport")):
            db.execute(
                """INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,
                   source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,
                   verification_status,source_batch,chain_name,chain_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (property_id, name, "https://chain.example", "chain.example", "tourism=hotel", "Example City",
                 "OpenStreetMap", f"https://www.openstreetmap.org/{property_id.removeprefix('osm-hotel:')}",
                 property_id, "ODbL 1.0", "2026-10-21T00:00:00Z", "DISCOVERED_UNVERIFIED", "fixture.pbf",
                 "Example Hotels", "CHAIN_INDICATED"),
            )

    summary = hotel_summary(database)

    assert summary["property_candidates"] == 2
    assert summary["unique_domains"] == 1
    assert summary["chain_indicated_properties"] == 2
    assert summary["pending_verification"] == 2
    exported = tmp_path / "hotels.csv"
    export_hotels(database, exported)
    import csv
    with exported.open(encoding="utf-8-sig", newline="") as stream:
        exported_rows = list(csv.DictReader(stream))
    assert len(exported_rows) == 2
    assert all(row["candidate_classifications"] == "BUSINESS;HOTEL" for row in exported_rows)
    assert all(row["hotel_cohort_member"] == "YES" for row in exported_rows)
    assert all(row["smb_cohort_member"] == "NO_BY_DEFAULT" for row in exported_rows)


def test_geo_platform_profile_contract_is_evidence_backed_and_deterministic(tmp_path):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    with sqlite3.connect(database) as db:
        db.execute("""INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,source_name,
          source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,
          geo_eligibility_status,geo_opportunity_status,qualification_evidence,evidence_json,last_inspected_at,
          property_type,source_batch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          ("osm-hotel:node/1", "Test Hotel", "https://test.example", "test.example", "OSM",
           "https://www.openstreetmap.org/node/1", "node/1", "ODbL", "2026-01-01", "VERIFIED", "ELIGIBLE",
           "HIGH", json.dumps({"positive": ["evidence"]}),
           json.dumps({"pages": [{"final_url": "https://test.example/stay", "title": "Rooms", "http_status": 200, "content_hash": "abc"}]}),
           "2026-01-02", "hotel", "test"))
        db.commit()
    output = tmp_path / "profiles.jsonl"
    export_geo_platform_profiles(database, output)
    first = output.read_text()
    export_geo_platform_profiles(database, output)
    assert output.read_text() == first
    profile = json.loads(first)
    assert profile["schema_version"] == "geo-company-profile/v1"
    assert profile["classifications"] == ["BUSINESS", "HOTEL"]
    assert profile["cohort_membership"]["hotel"] is True
    assert profile["cohort_membership"]["smb"] is False
    assert profile["identity"]["verification_status"] == "VERIFIED"
    assert profile["website_evidence"][0]["content_hash"] == "abc"
    assert profile["review"]["outreach_ready_status"] == "NOT_READY"
    assert profile["review"]["website_changes_applied"] is False


def test_geo_platform_profile_export_excludes_unverified_candidates(tmp_path):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    with sqlite3.connect(database) as db:
        db.execute("""INSERT INTO candidates(candidate_id,company_name,source_name,source_url,source_record_id,
          source_license_or_permission,discovery_timestamp,source_batch) VALUES(?,?,?,?,?,?,?,?)""",
          ("osm-hotel:node/2", "Unverified", "OSM", "https://www.openstreetmap.org/node/2", "node/2", "ODbL", "2026-01-01", "test"))
        db.commit()
    output = tmp_path / "profiles.jsonl"
    export_geo_platform_profiles(database, output)
    assert output.read_text() == ""


def test_cohort_membership_export_is_read_only_property_level_and_stable(tmp_path):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    with sqlite3.connect(database) as db:
        db.execute("""INSERT INTO candidates(candidate_id,company_name,canonical_domain,source_name,source_url,
          source_record_id,source_license_or_permission,discovery_timestamp,source_batch,chain_name,chain_status)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          ("osm-hotel:node/7", "Chain Downtown", "chain.example", "OSM", "https://osm/node/7", "node/7",
           "ODbL", "2026-10-21", "test", "Example Chain", "CHAIN_INDICATED"))
    before = database.read_bytes()
    output = tmp_path / "membership.csv"
    export_hotel_cohort_membership(database, output)
    first = output.read_bytes()
    export_hotel_cohort_membership(database, output)
    assert output.read_bytes() == first
    assert database.read_bytes() == before
    import csv
    with output.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["candidate_id"] == "osm-hotel:node/7"
    assert row["candidate_classifications"] == "BUSINESS;HOTEL"
    assert row["hotel_cohort_member"] == "YES"
    assert row["smb_cohort_member"] == "NO_BY_DEFAULT"
    assert row["identity_link_status"] == "PROPERTY_RECORD;COMPANY_LINK_NOT_ASSERTED"


def test_startup_handoff_requires_yc_attribution_and_verified_eligibility(tmp_path):
    database = tmp_path / "phase7.db"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TABLE candidates(candidate_id TEXT,company_name TEXT,website TEXT,canonical_domain TEXT,
          source_name TEXT,verification_status TEXT,geo_eligibility_status TEXT,geo_opportunity_status TEXT,
          verification_reason TEXT,industry TEXT,location TEXT,qualification_evidence TEXT,evidence_json TEXT,
          source_url TEXT,source_record_id TEXT,source_license_or_permission TEXT,discovery_timestamp TEXT,
          last_inspected_at TEXT,human_review_status TEXT,outreach_ready_status TEXT)""")
        common = ("https://example.test", "example.test", "VERIFIED", "ELIGIBLE", "HIGH", "identity evidence",
                  "Software", "Boston", "{}", "{}", "https://source.test", "source-1", "recorded permission",
                  "2026-01-01", "2026-01-02", "PENDING", "NOT_READY")
        db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("yc-1", "YC Startup", common[0], common[1], "Y Combinator Public Startup Directory", *common[2:]))
        db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("osm-1", "OSM Shop", common[0], common[1], "OpenStreetMap", *common[2:]))
        db.commit()
    output = tmp_path / "startups.jsonl"
    export_startup_geo_profiles(database, output)
    profile = json.loads(output.read_text())
    assert profile["profile_type"] == "startup_company"
    assert profile["startup"]["size_evidence_status"] == "SIZE_UNKNOWN"
    assert profile["review"]["outreach_ready_status"] == "NOT_READY"


def test_hotel_worker_pauses_on_access_block_degradation_not_small_batches():
    assert hotel_batch_quality_pause({"attempted": 25, "counts": {"ACCESS_BLOCKED": 12}})
    assert not hotel_batch_quality_pause({"attempted": 25, "counts": {"ACCESS_BLOCKED": 5}})
    assert not hotel_batch_quality_pause({"attempted": 10, "counts": {"ACCESS_BLOCKED": 10}})


def test_hotel_batch_selection_is_repeatable_domain_diverse_and_balanced(tmp_path):
    database = tmp_path / "hotels.db"
    init_hotel_db(database)
    with sqlite3.connect(database) as db:
        rows = [
            ("osm-hotel:node/1", "Alpha Hotel", "alpha.example", "INDEPENDENT_OR_UNKNOWN", "A"),
            ("osm-hotel:node/2", "Alpha Hotel Airport", "alpha.example", "CHAIN_INDICATED", "B"),
            ("osm-hotel:node/3", "Bravo Inn", "bravo.example", "CHAIN_INDICATED", "A"),
            ("osm-hotel:node/4", "Charlie Lodge", "charlie.example", "INDEPENDENT_OR_UNKNOWN", "C"),
        ]
        for candidate_id, name, domain, chain_status, location in rows:
            db.execute("""INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,
              source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,
              verification_status,source_batch,chain_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (candidate_id, name, f"https://{domain}", domain, "hotel", location, "OSM",
               f"https://www.openstreetmap.org/{candidate_id.removeprefix('osm-hotel:')}",
               candidate_id, "ODbL", "2026-10-21", "DISCOVERED_UNVERIFIED", "fixture", chain_status))
    first = select_hotel_pilot(database, 3)
    assert first == select_hotel_pilot(database, 3)
    assert len(first) == len(set(first)) == 3
    with sqlite3.connect(database) as db:
        domains = [db.execute("SELECT canonical_domain FROM candidates WHERE candidate_id=?", (cid,)).fetchone()[0] for cid in first]
        classes = [db.execute("SELECT chain_status FROM candidates WHERE candidate_id=?", (cid,)).fetchone()[0] for cid in first]
    assert len(domains) == len(set(domains))
    assert "CHAIN_INDICATED" in classes
    assert "INDEPENDENT_OR_UNKNOWN" in classes
