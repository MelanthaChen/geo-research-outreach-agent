import sqlite3

from outreach_agent.phase7_smb import (
    company_size_classification,
    contact_channel_classification,
    hotel_domain_overlap,
    phase7_smb_summary,
    reconcile_rows,
)


def _candidate(candidate_id, name, domain, *, industry="office=company", location="Boston, MA", verification="VERIFIED", geo="ELIGIBLE"):
    return {
        "candidate_id": candidate_id,
        "company_name": name,
        "website": f"https://{domain}",
        "canonical_domain": domain,
        "source_name": "source",
        "source_url": f"https://source/{candidate_id}",
        "source_record_id": candidate_id,
        "source_license_or_permission": "permission evidence",
        "discovery_timestamp": "2026-10-01T00:00:00Z",
        "industry": industry,
        "location": location,
        "verification_status": verification,
        "geo_eligibility_status": geo,
        "geo_opportunity_status": "HIGH",
        "qualification_evidence": "saved rationale",
        "evidence_json": '{"pages":[{"fetch_status":"SUCCESS","http_status":200,"visible_text_length":500}]}',
    }


def test_size_uses_saved_positive_count_and_existing_category_only():
    assert company_size_classification(10, "MICRO", "directory_reported") == "SMB_SUPPORTED"
    assert company_size_classification(0, "MICRO", "directory_reported") == "SIZE_UNKNOWN"
    assert company_size_classification(None, "UNKNOWN", "unknown") == "SIZE_UNKNOWN"
    assert company_size_classification(300, "MEDIUM", "directory_reported") == "CONFLICTING_EVIDENCE"


def test_channel_classification_counts_company_once_for_combined_channels():
    assert contact_channel_classification(True, False) == "EMAIL_ONLY"
    assert contact_channel_classification(False, True) == "FORM_ONLY"
    assert contact_channel_classification(True, True) == "EMAIL_AND_FORM"
    assert contact_channel_classification(False, False) == "NO_CONTACT"


def test_reconciliation_keeps_unknown_size_as_review_and_excludes_hotels():
    candidates = [
        _candidate("known", "Known Studio", "known.example"),
        _candidate("unknown", "Unknown Studio", "unknown.example"),
        _candidate("hotel", "Hotel Example", "hotel.example", industry="tourism=hotel"),
    ]
    active = {
        "known.example": {"company_size_value": 8, "company_size_category": "MICRO", "company_size_source": "directory_reported"},
        "unknown.example": {"company_size_value": None, "company_size_category": "UNKNOWN", "company_size_source": "unknown"},
    }
    contactable = {"known": {"candidate_id": "known"}, "unknown": {"candidate_id": "unknown"}, "hotel": {"candidate_id": "hotel"}}
    channels = {
        "known": [{"channel_classification": "PUBLIC_BUSINESS_EMAIL", "email": "hello@known.example", "review_status": "PENDING"}],
        "hotel": [{"channel_classification": "OFFICIAL_CONTACT_FORM", "channel_url": "https://hotel.example/contact", "review_status": "PENDING"}],
    }
    rows, summary = reconcile_rows(candidates, active, contactable, channels, {})
    by_id = {row["candidate_id"]: row for row in rows}
    assert by_id["known"]["contactable_smb"] == "YES"
    assert by_id["unknown"]["company_size_classification"] == "SIZE_UNKNOWN"
    assert by_id["unknown"]["smb_target_status"] == "NEEDS_REVIEW_SIZE"
    assert by_id["hotel"]["smb_target_status"] == "EXCLUDED_HOTEL_TARGET"
    assert summary["verified_smb_count"] == 1
    assert summary["geo_eligible_smb_count"] == 1
    assert summary["contactable_smb_count"] == 1


def test_same_name_and_location_is_flagged_not_merged():
    candidates = [_candidate("a", "Example Shop", "one.example"), _candidate("b", "Example Shop", "two.example")]
    rows, summary = reconcile_rows(candidates, {}, {}, {}, {})
    assert len(rows) == 2
    assert {row["identity_deduplication_status"] for row in rows} == {"POTENTIAL_DUPLICATE_NAME_REVIEW"}
    assert summary["unique_candidate_ids"] == 2
    assert summary["verified_smb_count"] == 0


def test_hotel_domain_overlap_is_a_review_lead_not_a_merged_company(tmp_path):
    database = tmp_path / "hotels.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE candidates(candidate_id TEXT,company_name TEXT,canonical_domain TEXT,verification_status TEXT,geo_eligibility_status TEXT,chain_name TEXT,chain_status TEXT)")
        connection.executemany("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)", [
            ("hotel-1", "Downtown Hotel", "chain.example", "VERIFIED", "ELIGIBLE", "Chain", "CHAIN_INDICATED"),
            ("hotel-2", "Airport Hotel", "chain.example", "VERIFIED", "ELIGIBLE", "Chain", "CHAIN_INDICATED"),
        ])
    rows, metrics = hotel_domain_overlap([{
        "candidate_id": "business-1", "company_name": "Chain Hotels Inc", "canonical_domain": "chain.example",
        "smb_target_status": "QUALIFIED_CONTACTABLE_SMB",
    }], database)
    assert len(rows) == 2
    assert all(row["identity_review_status"] == "PENDING_NOT_CONFIRMED" for row in rows)
    assert metrics["potential_shared_domains"] == 1
    assert metrics["contactable_smb_records_with_hotel_domain"] == 1
    assert metrics["confirmed_cross_cohort_company_links"] == 0


def test_saved_phase7_pool_reconciles_to_existing_strict_contactable_total():
    summary = phase7_smb_summary()
    assert summary["phase7_company_records"] == 57664
    assert summary["unique_candidate_ids"] == 57664
    assert summary["strict_contactable_companies"] == 7172
    assert summary["contact_channel_breakdown_contactable"] == {
        "EMAIL_ONLY": 4184,
        "FORM_ONLY": 2132,
        "EMAIL_AND_FORM": 856,
        "NO_CONTACT": 0,
    }
    assert summary["remaining_gap_to_5000"] == 5000 - summary["contactable_smb_count"]
