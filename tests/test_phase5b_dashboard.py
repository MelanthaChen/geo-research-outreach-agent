import hashlib
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from outreach_agent.dashboard import make_handler
from outreach_agent.dashboard_data import filter_companies, load_dashboard_data, make_draft_preview

ROOT = Path(__file__).resolve().parents[1]
FROZEN_DB = ROOT / "data/phase3c1_validation_77.db"
EXPERIMENTAL_DB = ROOT / "data/phase4f_experimental.db"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_db(path: Path) -> Path:
    path = path / "phase4f_experimental.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE companies (
              id INTEGER PRIMARY KEY, company_name TEXT, website TEXT, normalized_domain TEXT,
              industry TEXT, location TEXT, company_size_value INTEGER, qualification_evidence TEXT,
              qualification_reason TEXT, geo_opportunity_reason TEXT, eligibility TEXT, geo_opportunity TEXT,
              priority_tier TEXT, priority_score REAL, review_status TEXT, review_notes TEXT
            );
            CREATE TABLE contacts (
              id INTEGER PRIMARY KEY, company_id INTEGER, name TEXT, email TEXT, source_url TEXT,
              validation_status TEXT, review_status TEXT, channel_type TEXT, channel_url TEXT,
              purpose TEXT, evidence_text TEXT
            );
            CREATE TABLE discovery_records (
              id INTEGER PRIMARY KEY, company_id INTEGER, source_type TEXT, source_name TEXT,
              source_url TEXT, source_identifier TEXT
            );
            CREATE TABLE outreach (id INTEGER PRIMARY KEY, status TEXT);
            INSERT INTO companies VALUES
              (1,'Fixture Co','https://fixture.test','fixture.test','Software','Test City',NULL,
               '["Published product page", "Company descriptions"]','Accessible company evidence','Content gap evidence','ELIGIBLE','HIGH','HIGH',91,'PENDING',NULL),
              (2,'No Channel Co','https://no-channel.test','no-channel.test',NULL,NULL,NULL,NULL,NULL,NULL,'ELIGIBLE','UNKNOWN','NEEDS_REVIEW',NULL,'PENDING',NULL);
            INSERT INTO contacts VALUES
              (10,1,NULL,'hello@fixture.test','https://fixture.test/contact','PUBLISHED','PENDING','GENERAL_BUSINESS_EMAIL',NULL,'','Published contact address'),
              (20,2,NULL,NULL,'https://no-channel.test','UNKNOWN','PENDING','SUPPORT_EMAIL',NULL,'','Support only');
            INSERT INTO discovery_records VALUES (1,1,'YC','YC Public Directory','https://ycombinator.com/companies','fixture-id');
            """
        )
    return path


def verification_csv(path: Path) -> Path:
    path.write_text(
        "company_id,verification_status\n1,VERIFIED\n2,VERIFIED\n",
        encoding="utf-8",
    )
    return path


def test_dashboard_metrics_are_derived_from_existing_537_company_cohort():
    before = sha256(EXPERIMENTAL_DB)
    data = load_dashboard_data()
    with sqlite3.connect(f"file:{EXPERIMENTAL_DB.resolve()}?mode=ro", uri=True) as connection:
        assert data["summary"]["total_companies"] == connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 537
        assert data["summary"]["eligibility"]["ELIGIBLE"] == connection.execute("SELECT COUNT(*) FROM companies WHERE eligibility='ELIGIBLE'").fetchone()[0]
        assert data["summary"]["research_priority"]["HIGH"] == connection.execute("SELECT COUNT(*) FROM companies WHERE priority_tier='HIGH'").fetchone()[0]
        assert data["summary"]["outreach"]["real_outreach_records"] == connection.execute("SELECT COUNT(*) FROM outreach").fetchone()[0]
    assert sum(data["summary"]["website_verification"].values()) == 537
    assert sha256(EXPERIMENTAL_DB) == before


def test_dashboard_data_loading_is_stable_and_read_only():
    frozen_before = sha256(FROZEN_DB)
    experimental_before = sha256(EXPERIMENTAL_DB)
    first = load_dashboard_data()
    second = load_dashboard_data()
    assert first["summary"] == second["summary"]
    assert first["details"] == second["details"]
    assert sha256(FROZEN_DB) == frozen_before
    assert sha256(EXPERIMENTAL_DB) == experimental_before


def test_company_search_and_filters_use_existing_source_and_channel_evidence(tmp_path):
    database = fixture_db(tmp_path)
    verification = verification_csv(tmp_path / "verification.csv")
    data = load_dashboard_data(database, verification, tmp_path / "no-delivery.csv")
    filtered = filter_companies(data, {"q": "Fixture", "source": "YC Public Directory", "verification": "VERIFIED", "eligibility": "ELIGIBLE", "geo": "HIGH", "priority": "HIGH", "channel": "HAS_SUITABLE_CHANNEL"})
    assert [row["id"] for row in filtered] == [1]
    none = filter_companies(data, {"channel": "NO_SUITABLE_CHANNEL"})
    assert [row["id"] for row in none] == [2]
    assert filter_companies(data, {"q": "does not exist"}) == []


def test_company_detail_includes_provenance_evidence_missing_fields_and_reviews(tmp_path):
    data = load_dashboard_data(fixture_db(tmp_path), verification_csv(tmp_path / "verification.csv"), tmp_path / "empty.csv")
    detail = data["details"][1]
    assert detail["discovery_sources"] == ["YC Public Directory"]
    assert detail["qualification_evidence_items"] == ["Published product page", "Company descriptions"]
    assert detail["review_status"] == "PENDING"
    assert "Employee-size evidence is not recorded" in detail["missing_evidence"]
    assert detail["contact_channels"][0]["is_suitable_first_party"] is True


def test_draft_preview_uses_saved_recipient_and_marks_not_sent(tmp_path):
    data = load_dashboard_data(fixture_db(tmp_path), verification_csv(tmp_path / "verification.csv"), tmp_path / "empty.csv")
    draft = make_draft_preview(data, 1, 10)
    assert draft["status"] == "DRAFT_READY"
    assert draft["label"] == "DEMO DRAFT — NOT SENT"
    assert draft["recipient"] == "hello@fixture.test"
    assert draft["delivery_status"] == "NOT_SENT"
    assert draft["approval_status"] == "NOT_APPROVED"
    assert draft["subject"] == "Invitation to Participate in University Research on AI Search"
    assert "https://research.example.invalid/participation-interest" in draft["body"]
    assert "[RESEARCH CONTACT EMAIL NOT CONFIGURED]" in draft["body"]
    assert "[RESEARCHER NAME NOT CONFIGURED]" in draft["body"]
    assert "You may reply" not in draft["body"]
    assert draft["participation_interest_form_url"].startswith("DEMO PLACEHOLDER ONLY")
    assert "Research contact email" in draft["missing_configuration"]
    assert any("reserved .invalid demo placeholder" in item for item in draft["missing_configuration"])


def test_contact_form_dashboard_draft_uses_concise_form_cta_and_question_contact_only():
    data = load_dashboard_data()
    detail = data["details"][105]
    channel = next(item for item in detail["contact_channels"] if item["channel_type"] == "CONTACT_FORM" and item["is_suitable_first_party"])
    draft = make_draft_preview(data, 105, int(channel["id"]))
    assert draft["channel_type"] == "CONTACT_FORM"
    assert "https://research.example.invalid/participation-interest" in draft["body"]
    assert "[RESEARCH CONTACT EMAIL NOT CONFIGURED]" in draft["body"]
    assert "For questions only" in draft["body"]
    assert "reply" not in draft["body"].casefold()
    assert len(draft["body"]) < 600


def test_missing_channel_explanation_and_invalid_channel_are_fail_closed(tmp_path):
    data = load_dashboard_data(fixture_db(tmp_path), verification_csv(tmp_path / "verification.csv"), tmp_path / "empty.csv")
    missing = make_draft_preview(data, 2, None)
    assert missing["status"] == "NO_SUITABLE_CHANNEL"
    assert "No address is inferred" in missing["reason"]
    invalid = make_draft_preview(data, 1, 20)
    assert invalid["status"] == "CHANNEL_NOT_FOUND"


def test_simulation_has_no_write_route_and_no_human_approval_mutation():
    data = load_dashboard_data()
    before = sha256(EXPERIMENTAL_DB)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(data))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(base + "/api/simulate", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 405
        response = urllib.request.urlopen(base + "/api/companies/5")
        assert response.status == 200
        assert b"PENDING" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert sha256(EXPERIMENTAL_DB) == before


def test_dashboard_assets_do_not_load_external_resources_or_submit_forms():
    html = (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    css = (ROOT / "src/outreach_agent/dashboard/style.css").read_text(encoding="utf-8")
    js = (ROOT / "src/outreach_agent/dashboard/app.js").read_text(encoding="utf-8")
    assert "https://" not in html
    assert "https://" not in css
    assert "fetch(" in js and "fetch(\"http" not in js
    assert "localStorage" not in js and "sessionStorage" not in js
    assert 'method:"POST"' in js
    assert "SIMULATED DELIVERY" in js
    assert 'value="INTERESTED"' in (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    assert 'value="DECLINED"' in (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    assert 'value="UNANSWERED"' in (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    assert "SIMULATED FORM OPENED" in js
    assert "SIMULATED INTEREST SUBMITTED" in js
    assert "SIMULATED RESEARCH-TEAM REVIEW" in js
    assert "SIMULATED FOLLOW-UP PROCESS BEGINS" in js


def test_company_explorer_uses_compact_responsive_rows_without_a_wide_table():
    html = (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    css = (ROOT / "src/outreach_agent/dashboard/style.css").read_text(encoding="utf-8")
    js = (ROOT / "src/outreach_agent/dashboard/app.js").read_text(encoding="utf-8")
    assert '<div id="company-rows" class="company-list" role="listbox"' in html
    assert "<table>" not in html
    assert 'role="option"' in js and 'aria-selected="${row.id === state.selectedId}"' in js
    assert "websiteDomain(row.website)" in js
    assert ".company-list { display: grid; overflow-y: auto; overflow-x: hidden;" in css
    assert ".company-row { width: 100%; min-width: 0; display: grid; grid-template-columns: minmax(0,1fr) auto;" in css
    assert ".company-name { display: block;" in css and "overflow-wrap: anywhere;" in css
    assert ".company-row { grid-template-columns: minmax(0,1fr);" in css
