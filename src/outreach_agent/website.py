from __future__ import annotations

import hashlib
import gzip
import json
import logging
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach_agent.config import WebsiteSettings
from outreach_agent.models import Company, WebsitePage, WebsiteSnapshot
from outreach_agent.normalization import normalize_domain

logger = logging.getLogger(__name__)
HIGH_VALUE_TERMS = ("about", "product", "service", "solution", "pricing", "blog", "resource", "faq", "contact")


@dataclass
class CollectionSummary:
    processed: int = 0
    fetched: int = 0
    cached: int = 0
    failed: int = 0
    blocked: int = 0


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def extract_page(markup: str, requested_url: str, final_url: str, status: int, content_type: str) -> tuple[WebsitePage, list[str]]:
    soup = BeautifulSoup(markup, "html.parser")
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    description_node = soup.select_one('meta[name="description" i]')
    canonical_node = soup.select_one('link[rel~="canonical" i]')
    text = " ".join(soup.get_text(" ", strip=True).split())[:100_000]
    schema_types: set[str] = set()
    # Parse JSON-LD from the original markup because scripts were removed above.
    original = BeautifulSoup(markup, "html.parser")
    for node in original.select('script[type="application/ld+json" i]'):
        try:
            data = json.loads(node.string or node.get_text())
            stack = data if isinstance(data, list) else [data]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    value = item.get("@type")
                    if isinstance(value, str):
                        schema_types.add(value)
                    elif isinstance(value, list):
                        schema_types.update(str(entry) for entry in value)
                    stack.extend(value for value in item.values() if isinstance(value, (dict, list)))
                elif isinstance(item, list):
                    stack.extend(item)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    links: list[str] = []
    origin_domain = normalize_domain(final_url)
    for anchor in soup.select("a[href]"):
        href = urljoin(final_url, str(anchor.get("href"))).split("#", 1)[0]
        label = f"{anchor.get_text(' ', strip=True)} {urlsplit(href).path}".lower()
        if href.startswith(("http://", "https://")) and normalize_domain(href) == origin_domain and any(term in label for term in HIGH_VALUE_TERMS):
            if href not in links:
                links.append(href)
    page = WebsitePage(
        requested_url=requested_url,
        final_url=final_url,
        http_status=status,
        content_type=content_type,
        title=title,
        meta_description=str(description_node.get("content"))[:4000] if description_node and description_node.get("content") else None,
        canonical_url=urljoin(final_url, str(canonical_node.get("href"))) if canonical_node and canonical_node.get("href") else None,
        visible_text=text,
        visible_text_length=len(text),
        content_hash=hashlib.sha256(markup.encode("utf-8", errors="replace")).hexdigest(),
        structured_data_types=json.dumps(sorted(schema_types)),
        fetch_status="SUCCESS",
    )
    return page, links


class WebsiteCollector:
    def __init__(self, settings: WebsiteSettings, client: httpx.Client | None = None, sleep=time.sleep):
        self.settings = settings
        self.sleep = sleep
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.retry_count + 1):
            try:
                with self.client.stream("GET", url) as response:
                    body = bytearray()
                    chunks = [response.content] if response.is_stream_consumed else response.iter_raw()
                    for chunk in chunks:
                        body.extend(chunk)
                        if len(body) > self.settings.max_response_bytes:
                            raise ValueError("response exceeded configured size limit")
                    raw = bytes(body)
                    encoding = response.headers.get("content-encoding", "").lower()
                    try:
                        if encoding == "gzip":
                            raw = gzip.decompress(raw)
                        elif encoding == "deflate":
                            raw = zlib.decompress(raw)
                    except (gzip.BadGzipFile, zlib.error, EOFError):
                        # Some intermediaries advertise compression after decoding.
                        pass
                    if len(raw) > self.settings.max_response_bytes:
                        raise ValueError("decoded response exceeded configured size limit")
                    headers = dict(response.headers)
                    headers.pop("content-encoding", None)
                    headers.pop("content-length", None)
                    return httpx.Response(
                        response.status_code,
                        headers=headers,
                        content=raw,
                        request=response.request,
                        extensions=response.extensions,
                    )
            except (httpx.RequestError, ValueError) as exc:
                last_error = exc
                if attempt < self.settings.retry_count:
                    self.sleep(self.settings.delay_between_requests_seconds)
        assert last_error is not None
        raise last_error

    def inspect(self, company: Company) -> WebsiteSnapshot:
        requested = company.website
        parsed = urlsplit(requested)
        base = f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}"
        robots_url = urljoin(base, "/robots.txt")
        sitemap_url = urljoin(base, "/sitemap.xml")
        robots_present = False
        sitemap_present = False
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            robots = self._request(robots_url)
            robots_present = robots.status_code == 200
            if robots_present:
                parser.parse(robots.text.splitlines())
                if not parser.can_fetch(self.settings.user_agent, requested):
                    return WebsiteSnapshot(
                        company_id=company.id, requested_url=requested, fetch_status="ROBOTS_DENIED",
                        robots_present=True, sitemap_present=False, error_type="ROBOTS_DENIED",
                        error_message="robots.txt disallows the requested homepage",
                    )
        except (httpx.RequestError, ValueError):
            pass
        self.sleep(self.settings.delay_between_requests_seconds)
        try:
            sitemap = self._request(sitemap_url)
            sitemap_present = sitemap.status_code == 200 and ("xml" in sitemap.headers.get("content-type", "").lower() or sitemap.text.lstrip().startswith("<"))
        except (httpx.RequestError, ValueError):
            pass

        snapshot = WebsiteSnapshot(
            company_id=company.id, requested_url=requested, fetch_status="FETCH_FAILED",
            robots_present=robots_present, sitemap_present=sitemap_present,
        )
        queue = [requested]
        seen: set[str] = set()
        homepage_links: list[str] = []
        while queue and len(snapshot.pages) < self.settings.max_pages_per_company:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if len(seen) > 1 and robots_present and not parser.can_fetch(self.settings.user_agent, url):
                continue
            if seen:
                self.sleep(self.settings.delay_between_requests_seconds)
            try:
                response = self._request(url)
                content_type = response.headers.get("content-type", "")
                if response.status_code in {403, 429}:
                    snapshot.fetch_status = "BLOCKED"
                    snapshot.http_status = response.status_code
                    snapshot.error_type = f"HTTP_{response.status_code}"
                    snapshot.error_message = "Remote site denied or rate-limited access; no bypass attempted"
                    break
                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {response.status_code}", request=response.request, response=response)
                if "html" not in content_type.lower():
                    raise ValueError(f"non-HTML content type: {content_type or 'unknown'}")
                page, links = extract_page(response.text, url, str(response.url), response.status_code, content_type)
                snapshot.pages.append(page)
                if len(snapshot.pages) == 1:
                    homepage_links = links
                    queue.extend(links[: max(0, self.settings.max_pages_per_company - 1)])
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError, UnicodeError) as exc:
                snapshot.error_type = type(exc).__name__
                snapshot.error_message = str(exc)[:1000]
                logger.warning("stage=website company_id=%s url=%s error=%s", company.id, url, exc)
                if not snapshot.pages:
                    snapshot.fetch_status = "FETCH_FAILED"
                continue

        if snapshot.pages:
            homepage = snapshot.pages[0]
            all_types = sorted({kind for page in snapshot.pages for kind in json.loads(page.structured_data_types)})
            paths = " ".join(urlsplit(page.final_url or page.requested_url).path.lower() for page in snapshot.pages)
            total_text = sum(page.visible_text_length for page in snapshot.pages)
            signals: dict[str, Any] = {
                "reachable": True,
                "meaningful_text": homepage.visible_text_length >= 500,
                "substantial_public_information": total_text >= 1500,
                "product_or_service_page": any(term in paths for term in ("product", "service", "solution")),
                "informational_content": any(term in paths for term in ("blog", "resource", "about")),
                "faq_page": "faq" in paths or "FAQPage" in all_types,
                "contact_page": "contact" in paths,
                "pricing_page": "pricing" in paths,
                "structured_data": bool(all_types),
                "online_discovery_relevance": total_text >= 1000 and bool(homepage_links),
                "thin_content_opportunity": 100 <= homepage.visible_text_length < 1000,
                "pages_inspected": len(snapshot.pages),
            }
            snapshot.fetch_status = "SUCCESS" if not snapshot.error_type else "PARTIAL"
            snapshot.final_url = homepage.final_url
            snapshot.http_status = homepage.http_status
            snapshot.content_type = homepage.content_type
            snapshot.title = homepage.title
            snapshot.meta_description = homepage.meta_description
            snapshot.canonical_url = homepage.canonical_url
            snapshot.visible_text_length = homepage.visible_text_length
            snapshot.content_hash = homepage.content_hash
            snapshot.structured_data_types = json.dumps(all_types)
            snapshot.extracted_signals = json.dumps(signals, sort_keys=True)
        return snapshot


def collect_websites(
    session: Session,
    settings: WebsiteSettings,
    *,
    limit: int | None = None,
    refresh: bool = False,
    collector: WebsiteCollector | None = None,
) -> CollectionSummary:
    summary = CollectionSummary()
    companies = session.scalars(select(Company).order_by(Company.id).limit(limit or settings.max_companies_per_run)).all()
    worker = collector or WebsiteCollector(settings)
    try:
        for company in companies:
            summary.processed += 1
            latest = session.scalar(
                select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id).order_by(WebsiteSnapshot.fetched_at.desc())
            )
            fresh = latest and _utc(latest.fetched_at) >= datetime.now(timezone.utc) - timedelta(hours=settings.cache_ttl_hours)
            if fresh and not refresh:
                latest.used_cache = True
                summary.cached += 1
                logger.info("stage=website decision=cached company_id=%s snapshot_id=%s", company.id, latest.id)
                continue
            snapshot = worker.inspect(company)
            session.add(snapshot)
            session.flush()
            if snapshot.fetch_status == "BLOCKED":
                summary.blocked += 1
            elif snapshot.fetch_status in {"FETCH_FAILED", "ROBOTS_DENIED"}:
                summary.failed += 1
            else:
                summary.fetched += 1
            logger.info("stage=website decision=%s company_id=%s snapshot_id=%s", snapshot.fetch_status, company.id, snapshot.id)
        session.commit()
    finally:
        if collector is None:
            worker.close()
    return summary
