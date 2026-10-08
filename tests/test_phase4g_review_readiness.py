import csv
import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import httpx

from outreach_agent.config import WebsiteSettings
from outreach_agent.models import Company, WebsiteSnapshot
from outreach_agent.website import WebsiteCollector, collect_websites

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("phase4g_review_readiness", ROOT / "scripts" / "phase4g_review_readiness.py")
phase4g = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(phase4g)


def test_review_task_classification_keeps_tasks_independent_and_never_approves_outreach():
    verified = phase4g.classify_review_tasks(
        identity_status="VERIFIED", eligibility="ELIGIBLE", geo_opportunity="HIGH",
        company_review_status="PENDING", contact_status="NOT_RUN", contactability_status="NO_SUITABLE_CHANNEL",
        pending_suitable_channels=0,
    )
    assert verified["identity_review_status"] == "NOT_REQUIRED_MACHINE_VERIFIED"
    assert verified["eligibility_review_status"] == "MACHINE_SUPPORTED_PENDING_COMPANY_REVIEW"
    assert verified["geo_review_status"] == "MACHINE_SCORED_PENDING_COMPANY_REVIEW"
    assert verified["contact_channel_review_status"] == "NOT_DISCOVERED"
    assert verified["outreach_approval_status"] == "NOT_GRANTED"

    unresolved = phase4g.classify_review_tasks(
        identity_status="ACCESS_BLOCKED", eligibility="NEEDS_REVIEW", geo_opportunity="UNKNOWN",
        company_review_status="PENDING", contact_status="CONTACT_FOUND", contactability_status="READY_FOR_REVIEW",
        pending_suitable_channels=1,
    )
    assert unresolved["identity_review_status"] == "PENDING"
    assert unresolved["eligibility_review_status"] == "PENDING"
    assert unresolved["geo_review_status"] == "PENDING"
    assert unresolved["contact_channel_review_status"] == "WAITING_FOR_IDENTITY_VERIFICATION"
    assert unresolved["outreach_approval_status"] == "NOT_GRANTED"
    assert phase4g.classify_outreach_readiness(
        identity_status="ACCESS_BLOCKED", eligibility="ELIGIBLE", company_review_status="APPROVED",
        contact_channel_review_status="PENDING", contactability_status="READY_FOR_REVIEW", contact_status="CONTACT_FOUND",
    ) == "BLOCKED_IDENTITY_REVIEW"
    assert phase4g.classify_outreach_readiness(
        identity_status="VERIFIED", eligibility="ELIGIBLE", company_review_status="PENDING",
        contact_channel_review_status="NO_PENDING_SUITABLE_CHANNEL", contactability_status="READY_FOR_REVIEW", contact_status="CONTACT_FOUND",
    ) == "BLOCKED_COMPANY_REVIEW"


def test_priority_key_follows_identity_eligibility_geo_priority_and_stable_id():
    def row(company_id, identity="VERIFIED", eligibility="ELIGIBLE", geo="HIGH", priority="HIGH", confidence="HIGH"):
        return {"company_id": str(company_id), "identity_verification_status": identity, "eligibility": eligibility,
                "geo_opportunity": geo, "research_priority": priority, "_confidence": confidence}
    ordered = sorted([
        row(1, identity="AMBIGUOUS_IDENTITY"), row(3, geo="MEDIUM"), row(2),
    ], key=phase4g._priority_key)
    assert [r["company_id"] for r in ordered] == ["2", "3", "1"]


def test_scale_projection_uses_linear_inputs_and_does_not_invent_review_time():
    projection = phase4g.project_scale(1000, measured_companies=100, measured_runtime_seconds=600,
                                       minimum_requests=250, review_basis_size=100,
                                       review_counts={"identity": 12, "eligibility": 8, "geo": 3, "contact": 2, "company": 100})
    assert projection["processing_runtime_estimate_minutes"] == "100.00"
    assert projection["website_requests_minimum_estimate"] == "2500"
    assert projection["identity_review_task_estimate"] == "120"
    assert projection["company_review_decision_estimate"] == "1000"
    assert projection["human_review_minutes"] == "NOT_ESTIMATED_NO_OBSERVED_REVIEW_DURATION"
    assert projection["contact_channel_review_task_estimate"] == "20"
    projected_contact_unknown = phase4g.project_scale(1000, review_basis_size=100, review_counts={"contact": None})
    assert projected_contact_unknown["contact_channel_review_task_estimate"] == "NOT_EXTRAPOLATED_BOUNDED_CONTACT_SAMPLE"


def test_offline_exports_are_stable_unique_and_preserve_review_state(tmp_path):
    database = ROOT / "data" / "phase4f_experimental.db"
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    before = digest(database)
    counts = phase4g.write_outputs(database, tmp_path)
    first = {p.name: digest(p) for p in tmp_path.glob("*.csv")}
    phase4g.write_outputs(database, tmp_path)
    second = {p.name: digest(p) for p in tmp_path.glob("*.csv")}
    assert first == second
    assert counts["companies"] == 537
    with (tmp_path / "phase4g_review_queue.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    ids = [r["company_id"] for r in rows]
    assert len(ids) == len(set(ids)) == 537
    assert all(r["company_review_status"] == "PENDING" for r in rows)
    assert all(r["outreach_approval_status"] == "NOT_GRANTED" for r in rows)
    assert all(r["outreach_readiness_status"].startswith("BLOCKED_") for r in rows)
    assert all(not (r["required_review_action"] == "READY_FOR_OUTREACH" or r["outreach_approval_status"] == "APPROVED") for r in rows)
    assert digest(database) == before
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM outreach").fetchone()[0] == 0


def test_runtime_counters_include_retries_failures_cache_hits_and_per_company_time(session):
    company = Company(company_name="Runtime", normalized_name="runtime", website="https://runtime.test", normalized_domain="runtime.test")
    session.add(company)
    session.commit()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", headers={"content-type": "text/plain"})
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text="<urlset></urlset>", headers={"content-type": "application/xml"})
        return httpx.Response(200, text="<html><body>" + ("Useful company text " * 50) + "</body></html>", headers={"content-type": "text/html"})

    settings = WebsiteSettings(delay_between_requests_seconds=0, retry_count=1, max_pages_per_company=1)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    collector = WebsiteCollector(settings, client=client, sleep=lambda _: None)
    first = collect_websites(session, settings, collector=collector, company_ids=[company.id])
    assert first.website_requests_attempted == calls
    assert first.retry_attempts == 1
    assert first.website_requests_failed == 1
    assert first.website_requests_succeeded == calls - 1
    assert first.total_batch_runtime_seconds >= 0
    assert first.company_runtime_seconds[company.id] >= 0

    second = collect_websites(session, settings, collector=collector, company_ids=[company.id])
    assert second.cached == 1
    assert second.website_requests_attempted == 0
    assert second.retry_attempts == 0
    assert second.company_runtime_seconds[company.id] >= 0
    assert calls == first.website_requests_attempted
    collector.close()


def test_runtime_request_counter_includes_redirect_hops():
    def handler(request):
        if request.url.host == "first.test":
            return httpx.Response(302, headers={"location": "https://second.test/path"})
        return httpx.Response(200, text="ok")

    settings = WebsiteSettings(retry_count=0, max_response_bytes=1000)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    collector = WebsiteCollector(settings, client=client, sleep=lambda _: None)
    response = collector._request("https://first.test/")
    assert response.status_code == 200
    assert collector.website_requests_attempted == 2
    assert collector.website_requests_succeeded == 2
    assert collector.website_requests_failed == 0
    collector.close()
