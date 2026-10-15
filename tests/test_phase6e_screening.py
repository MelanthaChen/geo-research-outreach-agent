import csv
import sqlite3

from outreach_agent.phase6e_screening import (
    cheap_priority, create_working_copy, export_phase6e, list_phase6e_candidates,
    phase6e_summary, screen_all, stratified_sample, website_domain_concern,
)


def _source_db(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE candidates (
          candidate_id TEXT PRIMARY KEY, company_name TEXT, website TEXT, canonical_domain TEXT UNIQUE,
          industry TEXT, location TEXT, source_name TEXT, source_url TEXT, source_record_id TEXT,
          source_license_or_permission TEXT, discovery_timestamp TEXT, verification_status TEXT,
          geo_eligibility_status TEXT, geo_opportunity_status TEXT, contact_status TEXT,
          human_review_status TEXT, outreach_ready_status TEXT, verification_reason TEXT,
          qualification_evidence TEXT, fetch_status TEXT, http_status INTEGER, evidence_json TEXT,
          last_inspected_at TEXT, source_batch TEXT
        );
        CREATE TABLE candidate_provenance(candidate_id TEXT,source_name TEXT,source_url TEXT,source_record_id TEXT,source_license_or_permission TEXT,discovery_timestamp TEXT);
        """)
        for item in [
            ("a", "Local Bakery", "https://bakery.example.com", "bakery.example.com", "shop=bakery", "LIKELY_FIT", "DISCOVERED_UNVERIFIED", "UNKNOWN"),
            ("b", "History Museum", "https://museum.example.com", "museum.example.com", "tourism=museum", "UNCERTAIN", "VERIFIED", "ELIGIBLE"),
            ("c", "State Farm", "https://agent.example.com", "agent.example.com", "office=insurance", "LIKELY_FIT", "DISCOVERED_UNVERIFIED", "UNKNOWN"),
        ]:
            cid,name,website,domain,industry,_,verification,eligibility=item
            db.execute("INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,industry,location,source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,geo_eligibility_status,geo_opportunity_status,contact_status,human_review_status,outreach_ready_status,source_batch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid,name,website,domain,industry,"NY","OpenStreetMap","https://osm.org/x",f"node/{cid}","ODbL","2026-01-01",verification,eligibility,"UNKNOWN","NOT_DISCOVERED","PENDING","NOT_READY","test"))
            db.execute("INSERT INTO candidate_provenance VALUES(?,?,?,?,?,?)",(cid,"OSM","https://osm.org/x",f"node/{cid}","ODbL","2026-01-01"))


def test_conservative_category_rules_keep_institutions_uncertain_and_flag_chains():
    assert cheap_priority({"industry":"shop=books", "canonical_domain":"book.example.com", "company_name":"Bookshop"}, 1)[0] == "LIKELY_FIT"
    assert cheap_priority({"industry":"tourism=museum", "canonical_domain":"museum.org", "company_name":"Museum"}, 1)[0] == "UNCERTAIN"
    assert cheap_priority({"industry":"office=insurance", "canonical_domain":"agent.com", "company_name":"State Farm"}, 1)[3] == "LIKELY_CHAIN_OR_FRANCHISE"
    assert website_domain_concern("store.wixsite.com")=="HOSTED_PLATFORM_DOMAIN_REVIEW"


def test_full_screen_list_export_and_counts_are_stable(tmp_path):
    source=tmp_path/"source.db"; working=tmp_path/"working.db"; _source_db(source)
    create_working_copy(source,working)
    result=screen_all(working)
    assert result["screened"] == 3
    assert result["statuses"] == {"LIKELY_FIT":2,"UNCERTAIN":1}
    one=list_phase6e_candidates(q="bakery",db_path=working)
    assert one["total"] == 1 and one["candidates"][0]["company_name"] == "Local Bakery"
    uncertain=list_phase6e_candidates(screen_status="UNCERTAIN",source="OpenStreetMap",db_path=working)
    assert uncertain["total"]==1 and uncertain["candidates"][0]["candidate_id"]=="b"
    assert phase6e_summary(working)["candidate_count"] == 3
    out=tmp_path/"out"; counts=export_phase6e(working,out,top_n=2)
    assert counts["phase6e_top_5000_candidates.csv"] == 2
    with (out/"phase6e_top_5000_candidates.csv").open(encoding="utf-8-sig",newline="") as handle:
        rows=list(csv.DictReader(handle))
    assert len(rows)==2 and all(row["screen_status"]=="LIKELY_FIT" for row in rows)
    first=(out/"phase6e_top_5000_candidates.csv").read_bytes()
    export_phase6e(working,out,top_n=2)
    assert (out/"phase6e_top_5000_candidates.csv").read_bytes()==first


def test_deterministic_calibration_sample(tmp_path):
    source=tmp_path/"source.db"; working=tmp_path/"working.db"; _source_db(source)
    create_working_copy(source,working); screen_all(working)
    assert [x["candidate_id"] for x in stratified_sample(working,per_category=1)] == [x["candidate_id"] for x in stratified_sample(working,per_category=1)]
