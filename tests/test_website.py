import json
import gzip

import httpx
from sqlalchemy import func, select

from outreach_agent.config import WebsiteSettings
from outreach_agent.models import Company, WebsiteSnapshot
from outreach_agent.website import WebsiteCollector, collect_websites, extract_page


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
    first = collect_websites(session, settings, collector=collector)
    call_count = len(calls)
    second = collect_websites(session, settings, collector=collector)

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


def test_streamed_compressed_body_is_decoded_once():
    compressed = gzip.compress(HOME.encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=compressed, headers={"content-type": "text/html", "content-encoding": "gzip"})

    collector = make_collector(handler)
    response = collector._request("https://example.test")
    assert "Example Products" in response.text
    assert "content-encoding" not in response.headers
