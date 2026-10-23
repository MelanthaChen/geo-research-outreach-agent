import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from outreach_agent import dashboard


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
