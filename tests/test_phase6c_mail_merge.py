import csv
import hashlib
import io
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import pytest

from outreach_agent.mail_merge import (
    MAIL_MERGE_COLUMNS, TEST_RECIPIENTS, ExportLedger, assess_company_mail_merge,
    make_test_mail_merge, save_export,
)
from outreach_agent.dashboard import make_handler


def data_fixture(tmp_path: Path):
    db_path = tmp_path / "phase4f_experimental.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
        CREATE TABLE companies(id INTEGER PRIMARY KEY,company_name TEXT,website TEXT,normalized_domain TEXT,
          industry TEXT,location TEXT,company_size_value INTEGER,qualification_evidence TEXT,qualification_reason TEXT,
          geo_opportunity_reason TEXT,eligibility TEXT,geo_opportunity TEXT,priority_tier TEXT,priority_score REAL,
          review_status TEXT,review_notes TEXT);
        CREATE TABLE contacts(id INTEGER PRIMARY KEY,company_id INTEGER,name TEXT,email TEXT,source_url TEXT,
          validation_status TEXT,review_status TEXT,channel_type TEXT,channel_url TEXT,purpose TEXT,evidence_text TEXT);
        INSERT INTO companies VALUES(1,'Actual Business','https://actual.example','actual.example','Software','USA',NULL,
          'Evidence','Qualified reason','GEO evidence','ELIGIBLE','HIGH','HIGH',90,'APPROVED','Human decision');
        INSERT INTO contacts VALUES(11,1,NULL,'research@actual.example','https://actual.example/contact','VALID','APPROVED',
          'GENERAL_BUSINESS_EMAIL',NULL,'Published business contact','Published at first-party site');
        """)
    detail = {
        "id": 1, "company_name": "Actual Business", "website": "https://actual.example",
        "normalized_domain": "actual.example", "eligibility": "ELIGIBLE",
        "geographic_classification": "VERIFIED_US",
        "identity_verification_status": "VERIFIED", "review_status": "APPROVED",
        "contact_channels": [{"id": 11, "channel_type": "GENERAL_BUSINESS_EMAIL", "email": "research@actual.example",
                              "source_url": "https://actual.example/contact", "review_status": "APPROVED",
                              "is_suitable_first_party": True}],
    }
    data = {"details": {1: detail}}
    campaign_db = tmp_path / "campaigns.db"
    with sqlite3.connect(campaign_db) as db:
        db.executescript("""
        CREATE TABLE campaign_items(company_id INTEGER,campaign_id TEXT,response_status TEXT DEFAULT '');
        """)
    config_path = tmp_path / "outreach.yaml"
    config_path.write_text("""participation_interest_form_url: https://research.example/interest
research_contact_email: questions@example.edu
researcher_name: Researcher
research_team: Research Team
university_affiliation: University
subject_template: Invitation to Participate in University Research on AI Search
template_file: config/templates/research_invitation.txt
""", encoding="utf-8")
    suppression = tmp_path / "suppression.csv"
    suppression.write_text("email,reason,source\n", encoding="utf-8")
    token_path = tmp_path / "tokens.json"
    token_path.write_text(json.dumps({"1": "a" * 40}), encoding="utf-8")
    return data, campaign_db, config_path, suppression, token_path


def test_test_mail_merge_is_exactly_two_synthetic_rows_and_exact_addresses():
    content = make_test_mail_merge("test-campaign")
    rows = list(csv.DictReader(content.splitlines()))
    assert len(rows) == 2
    assert tuple(rows[0]) == MAIL_MERGE_COLUMNS
    assert [row["recipient_email"] for row in rows] == list(TEST_RECIPIENTS)
    assert [row["company_id"] for row in rows] == ["SYNTHETIC-001", "SYNTHETIC-002"]
    assert all("actual.example" not in row["email_body"] for row in rows)
    assert all("NOT A WORKING FORM" in row["participation_form_url"] and ".invalid" in row["participation_form_url"] for row in rows)
    assert all("NOT SENT" in row["email_body"] for row in rows)


def test_real_export_requires_all_gates_and_uses_https_company_token(tmp_path):
    data, campaign_db, config, suppression, tokens = data_fixture(tmp_path)
    result = assess_company_mail_merge(data, "campaign-a", [1], campaign_db,
        config_path=config, suppression_path=suppression, token_path=tokens)
    assert result["included_count"] == 1
    row = result["rows"][0]
    assert row["recipient_email"] == "research@actual.example"
    assert "https://research.example/interest?invitation_token=" + "a" * 40 in row["email_body"]
    assert ".invalid" not in row["email_body"]
    assert row["contact_evidence_url"] == "https://actual.example/contact"
    assert row["review_status"] == "COMPANY_AND_CONTACT_APPROVED"


def test_missing_form_or_review_blocks_real_mail_merge_without_placeholder(tmp_path):
    data, campaign_db, config, suppression, tokens = data_fixture(tmp_path)
    config.write_text(config.read_text().replace("https://research.example/interest", ""), encoding="utf-8")
    data["details"][1]["review_status"] = "PENDING"
    result = assess_company_mail_merge(data, "campaign-a", [1], campaign_db,
        config_path=config, suppression_path=suppression, token_path=tokens)
    assert result["included_count"] == 0
    assert result["exclusion_counts"]["COMPANY_HUMAN_REVIEW_INCOMPLETE"] == 1
    assert result["exclusion_counts"]["VALID_HTTPS_PARTICIPATION_FORM_NOT_CONFIGURED"] == 1
    assert result["rows"] == []
    assert result["preview_rows"][0]["review_status"] == "PREVIEW_ONLY_NOT_APPROVED"
    assert "PREVIEW ONLY — NOT READY TO SEND" in result["preview_rows"][0]["email_body"]
    assert ".invalid" in result["preview_rows"][0]["participation_form_url"]


def test_duplicate_suppressed_and_unapproved_company_are_excluded(tmp_path):
    data, campaign_db, config, suppression, tokens = data_fixture(tmp_path)
    with sqlite3.connect(campaign_db) as db:
        db.execute("INSERT INTO campaign_items(company_id,campaign_id) VALUES(1,'campaign-a')")
    suppression.write_text("email,reason,source\nresearch@actual.example,opt-out,manual\n", encoding="utf-8")
    data["details"][1]["contact_channels"][0]["review_status"] = "PENDING"
    result = assess_company_mail_merge(data, "campaign-a", [1], campaign_db,
        config_path=config, suppression_path=suppression, token_path=tokens)
    assert result["included_count"] == 0
    assert result["exclusion_counts"]["ALREADY_IN_SELECTED_CAMPAIGN"] == 1
    assert result["exclusion_counts"]["SUPPRESSED_EMAIL"] == 1
    assert result["exclusion_counts"]["CONTACT_HUMAN_REVIEW_INCOMPLETE"] == 1


def test_export_is_bom_stable_and_ledger_records_prepared_not_sent(tmp_path):
    content = make_test_mail_merge()
    path, file_id = save_export(tmp_path, "mailmerge.csv", content)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert hashlib.sha256(content.encode()).hexdigest() == file_id
    before = path.read_bytes()
    path_again, same_id = save_export(tmp_path, "mailmerge.csv", content)
    assert path_again.read_bytes() == before and same_id == file_id
    ledger_path = tmp_path / "ledger.db"
    ExportLedger(ledger_path).record(campaign_id="TEST", mode="TEST", rows=2, exclusions={}, file_id=file_id)
    with sqlite3.connect(ledger_path) as db:
        row = db.execute("select campaign_id,export_mode,row_count,file_id,record_status from exports").fetchone()
    assert row == ("TEST", "TEST", 2, file_id, "EXPORTED_PREPARED")


def test_dashboard_test_export_download_and_real_export_fail_closed(tmp_path, monkeypatch):
    data, campaign_db, _config, _suppression, _tokens = data_fixture(tmp_path)
    monkeypatch.setattr("outreach_agent.dashboard.DEFAULT_EXPORT_DIR", tmp_path / "exports")
    class ReadOnlyCampaignStub:
        path = campaign_db
        def get(self, campaign_id):
            if campaign_id != "campaign-a":
                raise KeyError(campaign_id)
            return {"id": campaign_id}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(data, ReadOnlyCampaignStub()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def post(path, payload):
        request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(request)
    try:
        response = post("/api/mail-merge/export", {"mode": "TEST", "campaign_id": "TEST-ONLY"})
        body = response.read()
        assert body.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        assert len(rows) == 2
        assert [row["recipient_email"] for row in rows] == list(TEST_RECIPIENTS)
        assert all(row["company_id"].startswith("SYNTHETIC-") for row in rows)
        assert b"real.example" not in body
        with pytest.raises(urllib.error.HTTPError) as error:
            post("/api/mail-merge/export", {"mode": "SELECTED", "campaign_id": "campaign-a", "company_ids": [1]})
        assert error.value.code == 409
        failure = json.loads(error.value.read())
        assert failure["included_count"] == 0
        assert "VALID_HTTPS_PARTICIPATION_FORM_NOT_CONFIGURED" in failure["exclusion_counts"]
        assert not (tmp_path / "exports" / "phase6c_mail_merge_campaign-a.csv").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
