import json
import gzip

import httpx
import pytest
from sqlalchemy import func, select

from outreach_agent.config import WebsiteSettings
from outreach_agent.models import Company, CompanyStatus, WebsiteSnapshot
from outreach_agent.website import RequestBudgetExceeded, WebsiteCollector, collect_websites, extract_page


HOME = """
<html><head><title>Example Products</title>
<meta name="description" content="Useful products for customers">
<link rel="canonical" href="https://example.test/">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"Organization"}}</script>
</head><body><main><h1>Example</h1><p>{text}</p>
<a href="/services">Our Services</a><a href="/faq">FAQ</a><a href="https://other.test/about">External</a>
</main></body></html>
""".format(text="customer information " * 80)


def test_extracts_metadata_text_links_and_jsonld():
    page, links = extract_page(HOME, "https://example.test", "https://example.test/", 200, "text/html")
    assert page.title == "Example Products"
    assert page.meta_description == "Useful products for customers"
    assert page.canonical_url == "https://example.test/"
    assert page.visible_text_length > 500
    assert json.loads(page.structured_data_types) == ["Organization"]
    assert links == ["https://example.test/services", "https://example.test/faq"]


def test_contact_and_partnership_links_are_prioritized_and_relative_urls_resolve():
    markup = """
    <html><body><main>
      <a href="/products">Products</a>
      <a href="/partner-with-us">Partner with us</a>
      <a href="contact-us">Contact</a>
    </main><footer><a href="/get-in-touch">Get in touch</a></footer></body></html>
    """
    _, links = extract_page(markup, "https://example.test/", "https://example.test/", 200, "text/html")
    assert links[0:3] == [
        "https://example.test/partner-with-us",
        "https://example.test/contact-us",
        "https://example.test/get-in-touch",
    ]


def test_extracts_channel_evidence_from_first_party_html():
    markup = """
    <html><body><section><h2>Partnerships</h2><p>Write to
      <a href="mailto:partners@example.test">partners@example.test</a></p></section>
    <section><h2>Contact Us</h2><form action="https://forms.example.test/contact">
      <input type="email" name="email"><textarea name="message"></textarea>
    </form></section></body></html>
    """
    page, _ = extract_page(markup, "https://example.test/contact", "https://example.test/contact", 200, "text/html")
    channels = json.loads(page.extracted_channels)
    assert any(row.get("email") == "partners@example.test" and row["type"] == "PARTNERSHIP_EMAIL" for row in channels)
    assert any(row.get("form") and row["url"] == "https://forms.example.test/contact" for row in channels)


def make_collector(handler) -> WebsiteCollector:
    settings = WebsiteSettings(
        delay_between_requests_seconds=0,
        retry_count=0,
        max_pages_per_company=3,
        max_response_bytes=100_000,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return WebsiteCollector(settings, client=client, sleep=lambda _: None)


def test_collection_stores_evidence_and_reuses_cache(session):
    company = Company(
        company_name="Example", normalized_name="example", website="https://example.test",
        normalized_domain="example.test",
    )
    session.add(company)
    session.commit()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", headers={"content-type": "text/plain"})
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text="<urlset></urlset>", headers={"content-type": "application/xml"})
        if request.url.path == "/":
            return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html><title>Detail</title><p>Helpful detail</p></html>", headers={"content-type": "text/html"})

    collector = make_collector(handler)
    settings = collector.settings
    first = collect_websites(session, settings, collector=collector, company_ids=[company.id])
    call_count = len(calls)
    second = collect_websites(session, settings, collector=collector, company_ids=[company.id])

    assert first.fetched == 1
    assert second.cached == 1
    assert len(calls) == call_count
    snapshot = session.scalar(select(WebsiteSnapshot))
    assert snapshot.fetch_status == "SUCCESS"
    assert snapshot.robots_present is True
    assert snapshot.sitemap_present is True
    assert len(snapshot.pages) == 3
    signals = json.loads(snapshot.extracted_signals)
    assert signals["meaningful_text"] is True
    assert signals["product_or_service_page"] is True


def test_collection_keeps_processing_after_unexpected_company_failure(session):
    companies = [
        Company(company_name=name, normalized_name=name.lower(), website=f"https://{name.lower()}.test", normalized_domain=f"{name.lower()}.test", pipeline_status=CompanyStatus.DISCOVERED)
        for name in ("Broken", "Healthy")
    ]
    session.add_all(companies)
    session.commit()

    class PartialFailureCollector:
        def inspect(self, company):
            if company.id == companies[0].id:
                raise RuntimeError("isolated failure")
            return WebsiteSnapshot(company_id=company.id, requested_url=company.website, fetch_status="SUCCESS")

    summary = collect_websites(session, WebsiteSettings(max_companies_per_run=2), collector=PartialFailureCollector())
    assert (summary.processed, summary.failed, summary.fetched) == (2, 1, 1)
    session.expire_all()
    snapshots = session.scalars(select(WebsiteSnapshot).order_by(WebsiteSnapshot.company_id)).all()
    assert [snapshot.fetch_status for snapshot in snapshots] == ["FETCH_FAILED", "SUCCESS"]


def test_targeted_collection_does_not_inspect_other_company_ids(session):
    companies = [Company(company_name=f"Example {n}", normalized_name=f"example {n}", website=f"https://example{n}.test", normalized_domain=f"example{n}.test") for n in (1, 2)]
    session.add_all(companies)
    session.commit()
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", headers={"content-type": "text/plain"})
        if request.url.path == "/sitemap.xml":
            return httpx.Response(404)
        return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})

    collector = make_collector(handler)
    summary = collect_websites(session, collector.settings, collector=collector, company_ids=[companies[1].id])
    assert summary.processed == 1
    assert all("example2.test" in url for url in requested)
    assert session.scalar(select(func.count(WebsiteSnapshot.id))) == 1


def test_robots_denial_is_recorded(session):
    company = Company(
        company_name="Private", normalized_name="private", website="https://private.test",
        normalized_domain="private.test",
    )
    session.add(company)
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /", headers={"content-type": "text/plain"})

    collector = make_collector(handler)
    summary = collect_websites(session, collector.settings, collector=collector)
    snapshot = session.scalar(select(WebsiteSnapshot))
    assert summary.failed == 1
    assert snapshot.fetch_status == "ROBOTS_DENIED"
    assert snapshot.error_type == "ROBOTS_DENIED"


def test_restricted_robots_endpoint_stops_without_fetching_site_pages(session):
    company = Company(company_name="Restricted robots", normalized_name="restricted robots",
                      website="https://restricted.test", normalized_domain="restricted.test")
    session.add(company)
    session.commit()
    requested = []
    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(403, text="Access denied", headers={"content-type": "text/plain"})
    collector = make_collector(handler)
    summary = collect_websites(session, collector.settings, collector=collector)
    snapshot = session.scalar(select(WebsiteSnapshot))
    assert summary.failed == 1
    assert snapshot.fetch_status == "ROBOTS_DENIED"
    assert snapshot.error_type == "ROBOTS_FILE_RESTRICTED"
    assert snapshot.http_status == 403
    assert requested == ["/robots.txt"]


def test_blocked_site_does_not_crash_run(session):
    company = Company(
        company_name="Blocked", normalized_name="blocked", website="https://blocked.test",
        normalized_domain="blocked.test",
    )
    session.add(company)
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/robots.txt", "/sitemap.xml"}:
            return httpx.Response(404, headers={"content-type": "text/plain"})
        return httpx.Response(403, text="blocked", headers={"content-type": "text/html"})

    collector = make_collector(handler)
    summary = collect_websites(session, collector.settings, collector=collector)
    assert summary.blocked == 1
    assert session.scalar(select(WebsiteSnapshot)).fetch_status == "BLOCKED"


def test_failed_contact_page_url_is_persisted_without_counting_as_fetched(session):
    company = Company(
        company_name="Example", normalized_name="example", website="https://example.test",
        normalized_domain="example.test",
    )
    session.add(company)
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/robots.txt", "/sitemap.xml"}:
            return httpx.Response(404, headers={"content-type": "text/plain"})
        if request.url.path == "/":
            return httpx.Response(200, text='<a href="/contact-us">Contact us</a>', headers={"content-type": "text/html"})
        return httpx.Response(403, text="blocked", headers={"content-type": "text/html"})

    collector = make_collector(handler)
    collect_websites(session, collector.settings, collector=collector)
    snapshot = session.scalar(select(WebsiteSnapshot))
    failed_page = next(page for page in snapshot.pages if page.fetch_status == "BLOCKED")
    assert failed_page.requested_url == "https://example.test/contact-us"
    assert failed_page.http_status == 403
    assert sum(page.fetch_status == "SUCCESS" for page in snapshot.pages) == 1
    assert snapshot.fetch_status == "PARTIAL"


def test_streamed_compressed_body_is_decoded_once():
    compressed = gzip.compress(HOME.encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=compressed, headers={"content-type": "text/html", "content-encoding": "gzip"})

    collector = make_collector(handler)
    response = collector._request("https://example.test")
    assert "Example Products" in response.text
    assert "content-encoding" not in response.headers


def test_deterministic_size_limit_failure_is_not_retried():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"x" * 100, headers={"content-type": "text/html"})
    settings = WebsiteSettings(delay_between_requests_seconds=0, retry_count=1, max_response_bytes=20)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    collector = WebsiteCollector(settings, client=client, sleep=lambda _: None)
    try:
        with pytest.raises(ValueError, match="response exceeded configured size limit"):
            collector._request("https://example.test")
        assert len(calls) == 1
        assert collector.retry_attempts == 0
    finally:
        client.close()


def test_hard_request_budget_counts_redirect_hops_before_sending():
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={"location":"/next"})
    settings=WebsiteSettings(delay_between_requests_seconds=0,retry_count=1,max_response_bytes=1000)
    client=httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False)
    collector=WebsiteCollector(settings,client=client,sleep=lambda _:None,request_budget=1)
    try:
        try:
            collector._request("https://example.test/start")
            assert False, "request budget should stop before the redirect target"
        except RequestBudgetExceeded:
            pass
        assert calls==["https://example.test/start"]
        assert collector.website_requests_attempted==1
        assert collector.request_budget_exhausted is True
    finally:
        client.close()


def test_bounded_max_redirect_is_counted_once_as_terminal_response():
    def handler(request):
        return httpx.Response(302, headers={"location": "/again"})
    settings = WebsiteSettings(delay_between_requests_seconds=0, retry_count=0, max_response_bytes=1000)
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    collector = WebsiteCollector(settings, client=client, sleep=lambda _: None, request_budget=6)
    try:
        response = collector._request("https://example.test/start")
        assert response.status_code == 302
        assert collector.website_requests_attempted == 6
        assert collector.website_requests_succeeded + collector.website_requests_failed == 6
    finally:
        client.close()
