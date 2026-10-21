import hashlib
import csv
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from outreach_agent.dashboard import make_handler
from outreach_agent.dashboard_data import _hotel_candidate_summary, filter_companies, load_dashboard_data, make_draft_preview
from outreach_agent.dashboard_data import COMPANY_EXPORT_COLUMNS, export_companies_csv

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


def test_separate_hotel_metrics_are_read_only_and_distinguish_channels(tmp_path):
    path = tmp_path / "hotel.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE candidates(candidate_id TEXT, verification_status TEXT, geo_eligibility_status TEXT, human_review_status TEXT, last_inspected_at TEXT);
          CREATE TABLE hotel_contact_channels(candidate_id TEXT, channel_type TEXT, email TEXT, channel_url TEXT, review_status TEXT);
          INSERT INTO candidates VALUES('a','VERIFIED','ELIGIBLE','PENDING','2026-01-01'),('b','ACCESS_BLOCKED','NEEDS_REVIEW','PENDING',NULL);
          INSERT INTO hotel_contact_channels VALUES('a','GENERAL_BUSINESS_EMAIL','hi@example.test','','PENDING'),('a','CONTACT_FORM','','https://example.test/contact','PENDING');
        """)
    before = sha256(path)
    assert _hotel_candidate_summary(path) == {"properties": 2, "processed": 1, "verified": 1,
        "geo_eligible": 1, "with_business_email": 1, "with_confirmed_form": 1, "pending_review": 2}
    assert sha256(path) == before


def test_dashboard_data_loading_is_stable_and_read_only():
    frozen_before = sha256(FROZEN_DB)
    experimental_before = sha256(EXPERIMENTAL_DB)
    first = load_dashboard_data()
    second = load_dashboard_data()
    first_summary = dict(first["summary"])
    second_summary = dict(second["summary"])
    first_hotel = first_summary.pop("hotel_candidate_pool")
    second_hotel = second_summary.pop("hotel_candidate_pool")
    first_smb = dict(first_summary["phase7_smb_target"])
    second_smb = dict(second_summary["phase7_smb_target"])
    first_overlap = first_smb.pop("hotel_cross_cohort_domain_overlap")
    second_overlap = second_smb.pop("hotel_cross_cohort_domain_overlap")
    # The hotel worker may advance its separate database between these read-only snapshots.
    assert first_hotel["properties"] == second_hotel["properties"]
    assert second_hotel["processed"] >= first_hotel["processed"]
    assert first_smb == second_smb
    first_summary["phase7_smb_target"] = first_smb
    second_summary["phase7_smb_target"] = second_smb
    assert first_summary == second_summary
    assert first_overlap["hotel_property_records_with_domains"] == second_overlap["hotel_property_records_with_domains"]
    assert second_overlap["hotel_records_sharing_business_domain"] >= first_overlap["hotel_records_sharing_business_domain"]
    assert first["details"] == second["details"]
    assert sha256(FROZEN_DB) == frozen_before
    assert sha256(EXPERIMENTAL_DB) == experimental_before


def test_phase6a_snapshot_is_supported_as_read_only_dashboard_database(tmp_path):
    database = tmp_path / "phase6a_experimental.db"
    database.write_bytes(EXPERIMENTAL_DB.read_bytes())
    before = sha256(database)
    data = load_dashboard_data(database)
    assert data["summary"]["total_companies"] == 537
    assert sha256(database) == before


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
    assert draft["label"] == "DRAFT · NOT SENT"
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
    assert "Test delivery recorded" in js
    assert 'value="INTERESTED"' in (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    assert 'value="DECLINED"' in (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    assert 'value="UNANSWERED"' in (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    assert "Test form opened" in js
    assert "Test interest submitted" in js
    assert "Test review recorded" in js
    assert "Test handoff recorded" in js


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


def test_company_export_is_stable_one_row_per_company_and_formula_safe(tmp_path):
    data = load_dashboard_data(fixture_db(tmp_path), verification_csv(tmp_path / "verification.csv"), tmp_path / "empty.csv")
    data["details"][1]["company_name"] = "=HYPERLINK(\"x\")"
    first = export_companies_csv(data)
    second = export_companies_csv(data)
    assert first == second
    rows = list(csv.DictReader(io.StringIO(first)))
    assert len(rows) == 2
    assert list(rows[0]) == list(COMPANY_EXPORT_COLUMNS)
    assert rows[0]["Company name"].startswith("'=")
    assert rows[0]["Source name"] == "YC Public Directory"
    assert rows[0]["Contact email"] == "hello@fixture.test"
    filtered = list(csv.DictReader(io.StringIO(export_companies_csv(data, {"eligibility": "ELIGIBLE"}))))
    assert [row["Company ID"] for row in filtered] == ["1", "2"]
    filtered = list(csv.DictReader(io.StringIO(export_companies_csv(data, {"source": "YC Public Directory"}))))
    assert [row["Company ID"] for row in filtered] == ["1"]


def test_company_export_endpoint_downloads_all_and_filtered_results(tmp_path):
    database = fixture_db(tmp_path)
    data = load_dashboard_data(database, verification_csv(tmp_path / "verification.csv"), tmp_path / "empty.csv")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(data))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        all_response = urllib.request.urlopen(base + "/api/companies/export.csv")
        all_rows = list(csv.DictReader(io.StringIO(all_response.read().decode("utf-8-sig"))))
        assert len(all_rows) == 2
        assert all_response.headers["Content-Disposition"] == 'attachment; filename="companies.csv"'
        filtered = urllib.request.urlopen(base + "/api/companies/export.csv?source=YC%20Public%20Directory")
        filtered_rows = list(csv.DictReader(io.StringIO(filtered.read().decode("utf-8-sig"))))
        assert [row["Company ID"] for row in filtered_rows] == ["1"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_company_export_handles_more_than_five_thousand_rows_without_truncation(tmp_path):
    base = load_dashboard_data(fixture_db(tmp_path), verification_csv(tmp_path / "verification.csv"), tmp_path / "empty.csv")
    companies = []
    details = {}
    channel_presence = {}
    for company_id in range(1, 5002):
        source = base["companies"][(company_id - 1) % 2]
        detail = dict(base["details"][int(source["id"])])
        company = dict(source)
        company["id"] = company_id
        detail["id"] = company_id
        details[company_id] = detail
        companies.append(company)
        channel_presence[company_id] = bool(base["channel_presence"][int(source["id"])])
    large = {**base, "companies": companies, "details": details, "channel_presence": channel_presence}
    exported = export_companies_csv(large)
    assert sum(1 for _ in csv.DictReader(io.StringIO(exported))) == 5001


def test_test_email_settings_and_exact_recipient_preview_are_available_without_oauth():
    from outreach_agent.email_test_settings import (
        AUTHORIZED_TEST_RECIPIENTS, PREFERRED_SENDER, load_test_email_settings,
    )

    config = load_test_email_settings()
    assert PREFERRED_SENDER == "yuxuan.chen.1031@gmail.com"
    assert AUTHORIZED_TEST_RECIPIENTS == (
        "yuxaun.chen.1031@outlook.com", "yuxuan.chen.1031@outlook.com",
    )
    assert config["authorized_test_recipients"] == list(AUTHORIZED_TEST_RECIPIENTS)
    assert config["send_enabled"] is False
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(load_dashboard_data()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        stored = json.load(urllib.request.urlopen(base + "/api/test-email/config"))
        assert stored["preferred_sender"] == PREFERRED_SENDER
        assert stored["authorized_test_recipients"] == list(AUTHORIZED_TEST_RECIPIENTS)
        assert stored["authenticated"] is False
        preview = json.load(urllib.request.urlopen(base + "/api/test-email/preview?recipient=yuxaun.chen.1031%40outlook.com"))
        assert preview["to"] == "yuxaun.chen.1031@outlook.com"
        assert preview["from"] == PREFERRED_SENDER
        assert preview["status"] == "TEST PREVIEW · NOT SENT"
        assert "not a discovered company" in preview["body"]
        assert preview["participation_form_url"] == "[PUBLIC HTTPS TEST FORM URL NOT CONFIGURED]"
        assert preview["form_ready"] == "false"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_test_email_backend_allowlist_and_missing_gmail_auth_fail_closed(tmp_path):
    from outreach_agent.email_test_settings import test_email_preview
    from outreach_agent.gmail_integration import EmailEventStore

    events = EmailEventStore(tmp_path / "test-email-events.sqlite3")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(load_dashboard_data(), email_event_store=events))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            base + "/api/test-email/send",
            data=json.dumps({"recipient": "other@example.com", "confirm": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 403

        request = urllib.request.Request(
            base + "/api/test-email/send",
            data=json.dumps({"recipient": "yuxaun.chen.1031@outlook.com", "preview_hash": test_email_preview("yuxaun.chen.1031@outlook.com")["preview_hash"], "confirm": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 503
        result = json.loads(error.value.read())
        assert result["sent"] is False
        assert result["provider_accepted"] is False
        assert "Google OAuth desktop-client JSON" in result["error"]
        assert "public HTTPS test participation form URL" in result["error"]
        assert events.recent() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_test_email_ui_shows_configuration_preview_and_disabled_send():
    html = (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/outreach_agent/dashboard/app.js").read_text(encoding="utf-8")
    assert "EMAIL CONFIGURATION" in html
    assert 'id="test-email-recipient"' in html
    assert 'id="test-email-preview"' in html
    assert 'id="send-test-email" disabled' in html
    assert "yuxaun.chen.1031@outlook.com" not in js  # settings are loaded from persisted backend config
    assert "!state.emailConfiguration?.send_enabled" in js
