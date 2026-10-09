import hashlib
import importlib.util
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from outreach_agent.campaigns import CampaignStore
from outreach_agent.dashboard import make_handler
from outreach_agent.dashboard_data import load_dashboard_data

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "data/phase3c1_validation_77.db"
EXPERIMENTAL = ROOT / "data/phase4f_experimental.db"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cohort_fixture(tmp_path: Path):
    db_path = tmp_path / "phase4f_experimental.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
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
              (1,'Fixture Co','https://fixture.test','fixture.test','Software','Test City',NULL,'["Evidence"]','Reason','GEO reason','ELIGIBLE','HIGH','HIGH',91,'PENDING',NULL),
              (2,'No Channel Co','https://no-channel.test','no-channel.test',NULL,NULL,NULL,NULL,NULL,NULL,'ELIGIBLE','UNKNOWN','NEEDS_REVIEW',NULL,'PENDING',NULL),
              (3,'Form Co','https://form.test','form.test','Software','Test City',NULL,'["Evidence"]','Reason','GEO reason','ELIGIBLE','HIGH','HIGH',90,'PENDING',NULL),
              (4,'Unverified Co','https://unknown.test','unknown.test','Software','Test City',NULL,'["Evidence"]','Reason','GEO reason','ELIGIBLE','HIGH','HIGH',90,'PENDING',NULL),
              (5,'Needs Review Co','https://review.test','review.test','Software','Test City',NULL,'["Evidence"]','Reason','GEO reason','NEEDS_REVIEW','LOW','LOW',40,'PENDING',NULL);
            INSERT INTO contacts VALUES
              (10,1,NULL,'hello@fixture.test','https://fixture.test/contact','PUBLISHED','PENDING','GENERAL_BUSINESS_EMAIL',NULL,'','Published address'),
              (20,2,NULL,NULL,'https://no-channel.test','UNKNOWN','PENDING','SUPPORT_EMAIL',NULL,'','Support only'),
              (30,3,NULL,NULL,'https://form.test/contact','PUBLISHED','PENDING','CONTACT_FORM','https://form.test/contact','Contact us','Official company contact form');
            INSERT INTO discovery_records VALUES (1,1,'YC','YC Public Directory','https://ycombinator.com/companies','fixture-id');
            """
        )
    verification = tmp_path / "verification.csv"
    verification.write_text(
        "company_id,verification_status\n1,VERIFIED\n2,VERIFIED\n3,VERIFIED\n4,INSUFFICIENT_EVIDENCE\n5,VERIFIED\n",
        encoding="utf-8",
    )
    return load_dashboard_data(db_path, verification, tmp_path / "empty-delivery.csv"), db_path


def test_campaign_routes_verified_email_form_queue_and_exclusions(tmp_path):
    data, source_db = cohort_fixture(tmp_path)
    before = sha(source_db)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "  Pilot   DEMO  ", [1, 2, 3, 4, 5, 1])
    assert campaign["company_count"] == 5
    by_id = {item["company_id"]: item for item in campaign["items"]}
    assert by_id[1]["item_status"] == "DRAFT_REVIEW"
    assert by_id[1]["draft"]["recipient"] == "hello@fixture.test"
    assert by_id[1]["draft"]["label"] == "DEMO DRAFT — NOT SENT"
    assert ".invalid/demo/" in by_id[1]["form_url"]
    assert by_id[2]["item_status"] == "EXCLUDED_NO_SUITABLE_CHANNEL"
    assert by_id[3]["item_status"] == "CONTACT_FORM_REVIEW"
    assert by_id[3]["recipient"] == ""
    assert by_id[4]["item_status"] == "EXCLUDED_IDENTITY_UNVERIFIED"
    assert by_id[5]["item_status"] == "EXCLUDED_NOT_ELIGIBLE"
    assert campaign["eligible_count"] == 2
    assert campaign["ineligible_count"] == 1
    assert campaign["unresolved_count"] == 2
    assert campaign["excluded_count"] == 3
    assert sha(source_db) == before


def test_persistent_idempotent_simulated_queue_and_resumable_retry(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    path = tmp_path / "campaigns.db"
    store = CampaignStore(path)
    campaign = store.create(data, "Retry DEMO", [1])
    campaign_id = campaign["id"]
    item_idempotency_key = campaign["items"][0]["idempotency_key"]
    assert item_idempotency_key
    store.approve_demo(campaign_id)
    first = store.simulate_batch(campaign_id, limit=99, outcomes={1: "FAILED"})
    assert first["processed"] == 1 and first["batch_limit"] == 10
    assert first["campaign"]["status"] == "RUNNING"
    reopened = CampaignStore(path)
    item = reopened.get(campaign_id)["items"][0]
    assert item["delivery_status"] == "SIMULATED_FAILED"
    assert item["delivery_attempts"] == 1
    second = reopened.simulate_batch(campaign_id, outcomes={1: "DELIVERED"})
    assert second["campaign"]["status"] == "COMPLETED"
    assert second["campaign"]["simulated_deliveries"] == 1
    assert second["campaign"]["items"][0]["idempotency_key"] == item_idempotency_key
    assert second["campaign"]["items"][0]["delivery_attempts"] == 2


def test_failed_demo_attempts_are_bounded_and_pause_cancel_revoke_tokens(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "Safety DEMO", [1])
    cid = campaign["id"]
    store.approve_demo(cid)
    store.simulate_batch(cid, outcomes={1: "FAILED"})
    store.simulate_batch(cid, outcomes={1: "FAILED"})
    snapshot = store.get(cid)
    assert snapshot["items"][0]["delivery_attempts"] == 2
    assert snapshot["status"] == "COMPLETED"
    assert store.analytics()["real_sends"] == 0
    stopped = store.global_emergency_stop(True)
    assert stopped["emergency_stopped"] is True
    assert store.global_emergency_stop(False)["campaigns_resumed"] is False
    store.set_status(cid, "CANCELLED")
    assert store.get(cid)["items"][0]["token_revoked"] == 1


def test_emergency_stop_cannot_bypass_explicit_campaign_approval(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "Unapproved DEMO", [1])
    cid = campaign["id"]
    store.global_emergency_stop(True)
    store.global_emergency_stop(False)
    assert store.get(cid)["status"] == "DRAFT"
    with pytest.raises(ValueError, match="approval is required"):
        store.simulate_batch(cid)


def test_campaign_pause_resume_preserves_persisted_queue_and_approval(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "Resume DEMO", [1])
    cid = campaign["id"]
    store.approve_demo(cid)
    store.simulate_batch(cid, outcomes={1: "FAILED"})
    store.set_status(cid, "PAUSED")
    with pytest.raises(ValueError, match="paused"):
        store.simulate_batch(cid)
    resumed = store.set_status(cid, "RESUMED")
    assert resumed["status"] == "RUNNING"
    assert resumed["items"][0]["delivery_attempts"] == 1
    final = store.simulate_batch(cid)
    assert final["campaign"]["status"] == "COMPLETED"
    assert final["campaign"]["simulated_deliveries"] == 1


def test_test_response_statuses_include_more_info_and_opt_out_without_real_suppression(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "Responses TEST", [1])
    store.approve_demo(campaign["id"])
    store.simulate_batch(campaign["id"])
    for outcome in ("MORE_INFO", "OPTED_OUT", "DECLINED", "UNANSWERED"):
        result = store.record_response(campaign["id"], 1, outcome)
        assert result["items"][0]["response_status"] == f"SIMULATED_{outcome}"
        assert store.analytics()["real_sends"] == 0
    last_event = store.get(campaign["id"])["audit"][-1]
    assert json.loads(last_event["details_json"])["suppression_registry_changed"] is False


def test_interest_form_token_duplicate_validation_review_and_handoff_are_demo_only(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "Interest DEMO", [1])
    cid = campaign["id"]
    store.approve_demo(cid)
    store.simulate_batch(cid)
    token = store.get(cid)["items"][0]["demo_token"]
    assert store.open_demo_form(token)["status"] == "SIMULATED_FORM_OPENED"
    values = {"company_name": "Fixture Co", "website": "https://fixture.test", "contact_name": "DEMO RESPONDENT",
              "contact_role": "Synthetic fixture", "business_email": "demo@example.invalid", "interest": "INTERESTED",
              "questions": "Synthetic question"}
    submission = store.submit_demo_interest(token, values)
    assert submission["status"] == "SIMULATED_SUBMISSION_RECORDED"
    duplicate = store.submit_demo_interest(token, values)
    assert duplicate["status"] == "DUPLICATE_IGNORED"
    with pytest.raises(ValueError, match="before follow-up"):
        store.begin_demo_handoff(submission["submission_id"])
    assert store.review_demo_submission(submission["submission_id"])["formal_consent"] is False
    handoff = store.begin_demo_handoff(submission["submission_id"])
    assert handoff == {"status": "SIMULATED_FOLLOWUP_STARTED", "message_sent": False, "formal_consent": False, "simulated": True}
    analytics = store.analytics()
    assert analytics["simulated_submissions"] == 1
    assert analytics["formal_consents"] == 0
    assert analytics["opens_or_clicks"] == "NOT_TRACKED"
    with pytest.raises(ValueError, match="Invalid, revoked, or expired"):
        store.submit_demo_interest("x" * 40, values)


def test_campaign_duplicate_prevention_across_campaigns_and_source_review_is_unchanged(tmp_path):
    data, source_db = cohort_fixture(tmp_path)
    before = sha(source_db)
    store = CampaignStore(tmp_path / "campaigns.db")
    first = store.create(data, "First DEMO", [1])
    second = store.create(data, "Second DEMO", [1])
    assert second["items"][0]["item_status"] == "EXCLUDED_ALREADY_CAMPAIGNED"
    store.approve_demo(first["id"])
    store.simulate_batch(first["id"])
    third = store.create(data, "Third DEMO", [1])
    assert third["items"][0]["item_status"] == "EXCLUDED_ALREADY_CAMPAIGNED"
    assert sha(source_db) == before


def test_suppressed_first_party_email_is_excluded_without_writing_suppression_file(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    suppression = tmp_path / "suppression.csv"
    suppression.write_text("email,reason,source\nhello@fixture.test,do not contact,review\n", encoding="utf-8")
    before = sha(suppression)
    store = CampaignStore(tmp_path / "campaigns.db", suppression_path=suppression)
    campaign = store.create(data, "Suppression DEMO", [1])
    assert campaign["items"][0]["item_status"] == "EXCLUDED_SUPPRESSED_OPT_OUT"
    assert campaign["items"][0]["draft"] is None
    assert sha(suppression) == before


def test_demo_exports_are_stable_and_mark_synthetic_events(tmp_path):
    spec = importlib.util.spec_from_file_location("phase5d_campaign_demo", ROOT / "scripts/phase5d_campaign_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    campaign = store.create(data, "Export DEMO", [1])
    store.approve_demo(campaign["id"])
    store.simulate_batch(campaign["id"])
    first = module.export(store, tmp_path)
    hashes = [sha(path) for path in first]
    second = module.export(store, tmp_path)
    assert [sha(path) for path in second] == hashes
    assert "DEMO ONLY" in first[0].read_text(encoding="utf-8")
    assert "source_data_mutated" in first[1].read_text(encoding="utf-8").splitlines()[0]


def test_demo_dashboard_api_is_persistent_same_origin_only_and_has_no_real_send_route(tmp_path):
    data, _ = cohort_fixture(tmp_path)
    store = CampaignStore(tmp_path / "campaigns.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(data, store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.dumps({"name": "API DEMO", "company_ids": [1]}).encode()
        request = urllib.request.Request(base + "/api/campaigns", data=payload, method="POST", headers={"Content-Type": "application/json"})
        created = json.load(urllib.request.urlopen(request))
        assert created["status"] == "DRAFT"
        assert json.load(urllib.request.urlopen(base + "/api/campaigns"))["campaigns"][0]["id"] == created["id"]
        page_one = json.load(urllib.request.urlopen(base + "/api/companies?page=1&page_size=2"))
        page_two = json.load(urllib.request.urlopen(base + "/api/companies?page=2&page_size=2"))
        assert page_one["total"] == page_two["total"] == 5
        assert len(page_one["companies"]) == len(page_two["companies"]) == 2
        all_matches = json.dumps({"name": "All pages DEMO", "all_matching": True, "filters": {"geo": "HIGH"}}).encode()
        all_request = urllib.request.Request(base + "/api/campaigns", data=all_matches, method="POST", headers={"Content-Type": "application/json"})
        all_campaign = json.load(urllib.request.urlopen(all_request))
        assert all_campaign["company_count"] == 3
        cross = urllib.request.Request(base + "/api/campaigns", data=payload, method="POST",
                                       headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(cross)
        assert error.value.code == 403
        unknown = urllib.request.Request(base + "/api/send", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(unknown)
        assert error.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_campaign_ui_uses_same_origin_demo_only_and_separate_review_states():
    html = (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/outreach_agent/dashboard/app.js").read_text(encoding="utf-8")
    assert "Campaign workspace" in html
    assert "No email provider" in html
    assert 'method:"POST"' in js
    assert "fetch(\"http" not in js
    assert "SIMULATED" in js and "NOT SENT" in js
    assert "data-submission-review" in js
    assert "formal consent: NO" in js
    assert "privacy_notice" in (ROOT / "config/outreach.yaml").read_text(encoding="utf-8")
    assert "localStorage" not in js and "sessionStorage" not in js


def test_phase5e_dashboard_has_independent_views_and_safe_test_form_tools():
    html = (ROOT / "src/outreach_agent/dashboard/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/outreach_agent/dashboard/app.js").read_text(encoding="utf-8")
    assert 'data-view="overview"' in html and 'data-view="companies"' in html
    assert all(f'data-view="{page}"' in html for page in ("campaigns", "interest-form", "responses", "analytics", "settings"))
    assert 'data-page="settings"' in html and 'class="sidebar"' in html
    assert "window.addEventListener(\"hashchange\",navigate)" in js
    assert 'id="select-all-matching"' in html and "state.selectedIds" in js
    assert 'id="form-token-select"' in html and 'id="submit-local-interest"' in html
    assert 'apiWrite("/api/demo/interest"' in js and 'apiWrite("/api/demo/form-open"' in js
    assert "TEST RECORD" in js and "real_sends" in js
    assert "fetch(\"http" not in js and "localStorage" not in js


def test_real_cohort_and_frozen_77_database_hashes_survive_campaign_processing(tmp_path):
    frozen_before, cohort_before = sha(FROZEN), sha(EXPERIMENTAL)
    data = load_dashboard_data()
    eligible_ids = [cid for cid, detail in data["details"].items()
                    if detail["eligibility"] == "ELIGIBLE" and detail["identity_verification_status"] == "VERIFIED"
                    and any(channel["is_suitable_first_party"] and channel["channel_type"] != "CONTACT_FORM"
                            for channel in detail["contact_channels"])]
    store = CampaignStore(tmp_path / "isolated-sidecar.db")
    campaign = store.create(data, "Hash check DEMO", [min(eligible_ids)])
    store.approve_demo(campaign["id"])
    store.simulate_batch(campaign["id"])
    assert sha(FROZEN) == frozen_before
    assert sha(EXPERIMENTAL) == cohort_before
