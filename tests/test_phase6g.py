import csv
import sqlite3

import pytest

from outreach_agent.candidate_pool import init_candidate_db
from outreach_agent.phase6f import initialize as initialize_phase6f
from outreach_agent.phase6g import (
    contact_audit_rows, csv_text, failure_category, record_channel_decision,
    record_company_review, review_rows,
)


def _fixture(tmp_path):
    source = tmp_path / "source.db"
    init_candidate_db(source)
    phase6f = tmp_path / "phase6f.db"
    initialize_phase6f(phase6f, source)
    with sqlite3.connect(phase6f) as db:
        for cid, name, domain, kind, email, url, evidence in [
            ("osm:a", "Alpha Co", "alpha.example", "GENERAL_BUSINESS_EMAIL", "hello@alpha.example", "https://alpha.example/contact", "Contact hello@alpha.example"),
            ("osm:a", "Alpha Co", "alpha.example", "GENERAL_BUSINESS_EMAIL", "hello@alpha.example", "https://alpha.example/about", "Contact hello@alpha.example"),
            ("osm:b", "Beta Co", "beta.example", "CONTACT_FORM", None, "https://beta.example/contact", "Name Email Message Submit"),
            ("osm:c", "Gamma Co", "gamma.example", "CONTACT_PAGE", None, "https://gamma.example/contact", "Contact details"),
            ("osm:d", "Delta Co", "delta.example", "CONTACT_FORM", None, "https://thirdparty.example/form", "Name Email Message Submit"),
        ]:
            db.execute("""INSERT INTO candidates(candidate_id,company_name,website,canonical_domain,source_name,source_url,source_record_id,source_license_or_permission,discovery_timestamp,verification_status,geo_eligibility_status,geo_opportunity_status,contact_status,source_batch)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO NOTHING""",
              (cid,name,"https://"+domain+"/",domain,"OSM source","https://osm.example/"+cid,cid,"ODbL","2026-01-01","VERIFIED","ELIGIBLE","HIGH","CONTACT_FOUND","test"))
            db.execute("INSERT OR IGNORE INTO phase6f_selection(candidate_id,selection_rank,selection_reason,selected_from,selected_at) VALUES(?,?,?,?,?)",(cid,1,"fixture","test","now"))
            db.execute("INSERT OR IGNORE INTO phase6f_contact_channels(candidate_id,channel_type,email,channel_url,source_url,evidence,purpose) VALUES(?,?,?,?,?,?,?)",(cid,kind,email,url,url,evidence,evidence))
        db.execute("UPDATE candidates SET verification_status='FETCH_FAILED',fetch_status='FETCH_FAILED',verification_reason='No usable HTML was captured (timed out)' WHERE candidate_id='osm:d'")
        db.commit()
    return phase6f, tmp_path / "review.db"


def test_contact_audit_classifies_and_semantically_deduplicates(tmp_path):
    db, review = _fixture(tmp_path)
    rows = contact_audit_rows(db, review)
    assert len(rows) == 4
    by_id = {row["candidate_id"]: row for row in rows}
    assert by_id["osm:a"]["channel_classification"] == "PUBLIC_BUSINESS_EMAIL"
    assert by_id["osm:a"]["duplicate_count"] == 1
    assert by_id["osm:b"]["channel_classification"] == "OFFICIAL_CONTACT_FORM"
    assert by_id["osm:c"]["channel_classification"] == "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD"
    assert by_id["osm:d"]["channel_classification"] == "INVALID_AMBIGUOUS_OR_UNSUPPORTED"
    assert by_id["osm:d"]["first_party_provenance"] == "NO"


def test_human_channel_decisions_are_separate_and_filterable(tmp_path):
    db, review = _fixture(tmp_path)
    target = next(row for row in contact_audit_rows(db, review) if row["candidate_id"] == "osm:a")
    record_channel_decision("osm:a", target["channel_key"], "APPROVED", "Evidence checked", db_path=db, review_path=review)
    approved = review_rows(db_path=db, review_path=review, status="APPROVED")
    assert len(approved) == 1
    assert approved[0]["review_status"] == "APPROVED"
    assert approved[0]["review_reason"] == "Evidence checked"
    with pytest.raises(ValueError, match="first-party"):
        bad = next(row for row in contact_audit_rows(db, review) if row["candidate_id"] == "osm:d")
        record_channel_decision("osm:d", bad["channel_key"], "APPROVED", db_path=db, review_path=review)
    with pytest.raises(ValueError, match="not outreach authorization"):
        record_company_review("osm:a", "APPROVED", db_path=db, review_path=review)
    record_company_review("osm:a", "FURTHER_REVIEW", "Confirm business status", db_path=db, review_path=review)
    assert next(r for r in contact_audit_rows(db, review) if r["candidate_id"] == "osm:a")["company_review_status"] == "FURTHER_REVIEW"


def test_failure_categories_distinguish_unresolved_access_and_network_types():
    assert failure_category("ACCESS_BLOCKED", "BLOCKED", 403, "denied")[0] == "HTTP_403_ACCESS_DENIED_UNSPECIFIED"
    assert failure_category("ACCESS_BLOCKED", "BLOCKED", 429, "rate limited")[0] == "HTTP_429_RATE_LIMITED"
    assert failure_category("FETCH_FAILED", "FETCH_FAILED", None, "nodename nor servname")[0] == "DNS_FAILURE"
    assert failure_category("FETCH_FAILED", "FETCH_FAILED", None, "timed out")[0] == "TIMEOUT"
    assert failure_category("FETCH_FAILED", "FETCH_FAILED", None, "certificate verify failed")[0] == "TLS_ERROR"
    assert failure_category("ROBOTS_DENIED", "ROBOTS_DENIED", None, "robots.txt")[0] == "ROBOTS_DENIED"
    assert failure_category("AMBIGUOUS_IDENTITY", "SUCCESS", 200, "not enough identity evidence")[0] == "IDENTITY_AMBIGUOUS"


def test_review_export_is_stable_and_contains_provenance(tmp_path):
    db, review = _fixture(tmp_path)
    first = csv_text(contact_audit_rows(db, review)).encode()
    second = csv_text(contact_audit_rows(db, review)).encode()
    assert first == second
    parsed = list(csv.DictReader(first.decode().splitlines()))
    assert parsed[0]["source_url"].startswith("https://osm.example/")
    assert parsed[0]["evidence_url"].startswith("https://")
    assert all(row["review_status"] == "PENDING" for row in parsed)
