import json
import threading
import urllib.request
import sqlite3
from http.server import ThreadingHTTPServer

from outreach_agent import dashboard


def test_post_form_confirmation_api_uses_isolated_fixture_and_stays_draft(tmp_path, monkeypatch):
    from outreach_agent.research_lifecycle import create_email_draft, create_post_form_confirmation_draft, create_questionnaire_invitation, get_invitation_reference, ingest_google_form_response, register_profile
    path = tmp_path / "registry.db"
    profile = {"schema_version":"geo-company-profile/v1","profile_id":"fixture-company","profile_type":"startup_company","identity":{"name":"Fixture Company","website":"https://fixture.test","verification_status":"VERIFIED"},"startup":{"location":"Boston, MA"},"qualification":{"eligibility":"ELIGIBLE"}}
    register_profile(profile, path)
    invitation_id, _ = create_questionnaire_invitation("fixture-company", secret="secret", path=path)
    reference = get_invitation_reference(invitation_id, path=path)
    draft = create_email_draft("fixture-company", "invitation@test.example", "Invitation", path=path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft))
        conn.execute("UPDATE research_records SET contact_verification_status='CONTACT_VERIFIED',contact_channels_json=? WHERE profile_id='fixture-company'", ('[{"channel_type":"GENERAL_BUSINESS_EMAIL","email":"official@test.example","review_status":"APPROVED"}]',))
    ingest_google_form_response({"Invitation Reference":reference,"Is your company interested in participating in this research?":"Yes","Does your company authorize our research team to conduct the research described above?":"Yes","Full Name of the Authorized Company Representative":"Rep","Job Title of the Authorized Company Representative":"CEO","Business Email Address of the Authorized Company Representative":"representative@test.example"}, path=path)
    submission_id = sqlite3.connect(path).execute("SELECT submission_id FROM questionnaire_submissions").fetchone()[0]
    monkeypatch.setenv("OUTREACH_DASHBOARD_TOKEN", "operator-token")
    monkeypatch.setattr(dashboard, "REGISTRY_DB", path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler({"summary": {}, "details": {}}))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/recruitment/post-form-confirmation", data=json.dumps({"submission_id":submission_id}).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer operator-token"}, method="POST")
        response = json.load(urllib.request.urlopen(request))
        assert response["status"] == "DRAFT"
        assert response["official_company_email"] == "official@test.example"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_dashboard_queue_and_public_projections_require_admin_session(monkeypatch):
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
        for path in ("/api/public/summary", "/api/public/research/records?page=1&page_size=1", "/api/research/records", "/api/summary"):
            with __import__("pytest").raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
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


def test_geo_result_auth_rejects_non_ascii_header_without_proxy_error(monkeypatch):
    """Malformed service credentials must produce 401, never a handler crash."""
    monkeypatch.setenv("GEO_ENGINE_HANDOFF_TOKEN", "ascii-service-token")
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler({"summary": {}, "details": {}}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/integration/results",
            data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer caf\N{LATIN SMALL LETTER E WITH ACUTE}"},
            method="POST",
        )
        with __import__("pytest").raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_public_operational_pages_are_private(monkeypatch):
    monkeypatch.setenv("OUTREACH_DASHBOARD_TOKEN", "operator-secret-sentinel")
    def records(*args, **kwargs):
        profile_type = kwargs.get("profile_type") or "business_company"
        return {"records": [{
            "profile_id": "public-1", "profile_type": profile_type, "name": "Public Example",
            "official_website": "https://example.com", "industry": "Software", "location": "Boston, MA",
            "identity_verification_status": "VERIFIED", "geographic_classification": "VERIFIED_US",
            "screening_status": "SCREENED", "recruitment_priority": "HIGH", "qualification_status": "QUALIFIED",
            "contact_channels_json": '[{"email":"private@example.com"}]', "consent_status": "CONSENTED",
        }], "total": 1}
    monkeypatch.setattr(dashboard, "list_records", records)
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler({"summary": {}, "details": {}}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in ("/api/public/companies", "/api/public/hotels", "/api/public/campaigns", "/api/public/responses", "/api/public/analytics", "/api/campaigns"):
            with __import__("pytest").raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            assert error.value.code == 401
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
