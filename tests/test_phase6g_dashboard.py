import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from outreach_agent import dashboard


def test_public_dashboard_queue_is_read_only_and_redacted(monkeypatch):
    monkeypatch.setenv("OUTREACH_DASHBOARD_TOKEN", "operator-secret-sentinel")
    monkeypatch.setattr(dashboard, "list_records", lambda *args, **kwargs: {
        "records": [{
            "profile_id": "company-1", "profile_type": "business_company", "name": "Public Example",
            "official_website": "https://example.com", "industry": "Software", "location": "Boston, MA",
            "identity_verification_status": "VERIFIED", "geographic_classification": "VERIFIED_US",
            "screening_status": "SCREENED", "recruitment_priority": "HIGH", "qualification_status": "QUALIFIED",
            "contact_channels_json": '[{"email":"private@example.com"}]', "consent_status": "CONSENTED",
            "respondent_name": "Private Person", "questionnaire_submissions": [{"contact_email": "private@example.com"}],
        }], "total": 1,
    })
    data = {"summary": {"total_companies": 1, "cloud_registry": True}, "filters": {}, "details": {}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(data))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        summary = json.load(urllib.request.urlopen(base + "/api/public/summary"))
        assert summary["public"] is True
        assert "contact_channel_coverage" in summary["summary"]
        assert summary["summary"]["contact_channel_coverage"] == {}
        queue = json.load(urllib.request.urlopen(base + "/api/public/research/records?page=1&page_size=1"))
        assert queue["public"] is True
        assert queue["records"][0]["name"] == "Public Example"
        assert "contact_channels_json" not in queue["records"][0]
        assert "consent_status" not in queue["records"][0]
        assert "questionnaire_submissions" not in queue["records"][0]
        with __import__("pytest").raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/research/records")
        assert error.value.code == 401
        with __import__("pytest").raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/summary")
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_questionnaire_ingestion_is_not_blocked_by_dashboard_auth(monkeypatch):
    monkeypatch.setenv("OUTREACH_DASHBOARD_TOKEN", "operator-secret-sentinel")
    monkeypatch.delenv("RESEARCH_QUESTIONNAIRE_SECRET", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler({"summary": {}, "details": {}}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/questionnaire",
            data=b"token=opaque-test-token&agree_research=true",
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        with __import__("pytest").raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_phase6g_dashboard_review_api_is_paginated_filterable_and_scoped(monkeypatch):
    row = {"candidate_id": "osm:one", "channel_key": "channel-key", "company_name": "Example",
           "source_name": "OSM", "confidence": "HIGH", "review_status": "PENDING",
           "company_review_status": "PENDING", "channel_classification": "PUBLIC_BUSINESS_EMAIL"}
    observed = {}
    monkeypatch.setattr(dashboard, "phase6g_review_rows", lambda **filters: (observed.setdefault("calls", []).append(filters) or [row]))
    monkeypatch.setattr(dashboard, "phase6g_csv_text", lambda rows: "candidate_id\r\nosm:one\r\n")
    decisions = []
    monkeypatch.setattr(dashboard, "phase6g_record_channel_decision", lambda *args, **kwargs: decisions.append((args, kwargs)))
    monkeypatch.setattr(dashboard, "phase6g_record_company_review", lambda *args, **kwargs: decisions.append((args, kwargs)))
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler({"summary": {}, "details": {}}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        payload = json.load(urllib.request.urlopen(base + "/api/phase6g/review?source=OSM&confidence=HIGH&page=1&page_size=1"))
        assert payload["total"] == 1 and payload["rows"][0]["candidate_id"] == "osm:one"
        assert {key: observed["calls"][0][key] for key in ("source", "confidence", "status")} == {"source": "OSM", "confidence": "HIGH", "status": ""}
        export = urllib.request.urlopen(base + "/api/phase6g/review/export.csv?source=OSM").read().decode("utf-8-sig")
        assert "osm:one" in export
        request = urllib.request.Request(base + "/api/phase6g/review/channel", data=json.dumps({
            "candidate_id": "osm:one", "channel_key": "channel-key", "decision": "APPROVED", "note": "reviewed",
        }).encode(), headers={"Content-Type": "application/json"}, method="POST")
        result = json.load(urllib.request.urlopen(request))
        assert result["saved"] is True
        assert result["scope"] == "CONTACT_CHANNEL_ONLY_NOT_OUTREACH_AUTHORIZATION"
        assert decisions[0][0] == ("osm:one", "channel-key", "APPROVED", "reviewed")
        assert decisions[0][1]["db_path"] == (dashboard.PHASE6H_DB if dashboard.PHASE6H_DB.exists() else dashboard.PHASE6F_DB)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cloud_sender_accounts_bootstrap_is_non_failing(monkeypatch):
    monkeypatch.setenv("OUTREACH_DATABASE_URL", "postgresql://cloud-registry.example/outreach")
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler({"summary": {}, "details": {}}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.load(urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/sender-accounts"))
        assert payload["accounts"] == []
        assert payload["storage"] == "cloud_registry_not_configured"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
