import html
import json

import httpx
import pytest

from outreach_agent.discovery import YCPublicDirectorySource


def test_yc_detail_parser_preserves_public_provenance():
    payload = {
        "props": {
            "company": {
                "id": 42,
                "slug": "sample-co",
                "name": "Sample Co",
                "website": "https://sample.example/",
                "tags": ["Consumer"],
                "location": "Boston, MA",
                "team_size": 12,
                "ycdc_status": "Active",
            }
        }
    }
    markup = f'<div data-page="{html.escape(json.dumps(payload), quote=True)}"></div>'
    candidate = YCPublicDirectorySource._parse_detail(markup, "https://www.ycombinator.com/companies/sample-co")
    assert candidate is not None
    assert candidate.company_name == "Sample Co"
    assert candidate.source_identifier == "42"
    assert candidate.source_name == "Y Combinator Consumer Startup Directory"
    assert candidate.source_url.endswith("/sample-co")
    assert candidate.customer_facing is False
    assert candidate.search_dependent is False


def test_yc_detail_parser_skips_company_without_website():
    payload = {"props": {"company": {"id": 1, "name": "No Site"}}}
    markup = f'<div data-page="{html.escape(json.dumps(payload), quote=True)}"></div>'
    assert YCPublicDirectorySource._parse_detail(markup, "https://example.test/company") is None


def test_yc_source_checks_robots_and_honors_limit():
    payload = {"props": {"company": {"id": 7, "name": "Public Co", "website": "https://public.example", "team_size": 2, "ycdc_status": "Active"}}}
    detail = f'<div data-page="{html.escape(json.dumps(payload), quote=True)}"></div>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /companies/industry/\n")
        if request.url.path == "/companies/industry/consumer":
            return httpx.Response(200, text='<a href="/companies/public-co">Public</a>')
        return httpx.Response(200, text=detail)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = YCPublicDirectorySource(limit=1, delay_seconds=0, client=client)
    assert [candidate.company_name for candidate in source.discover()] == ["Public Co"]


def test_yc_source_stops_when_robots_disallows_directory():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /companies/\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = YCPublicDirectorySource(limit=1, delay_seconds=0, client=client)
    with pytest.raises(ValueError, match="robots.txt disallows"):
        list(source.discover())
