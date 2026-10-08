from __future__ import annotations

import csv
import html
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
from pydantic import BaseModel, ConfigDict, field_validator


class CompanyCandidate(BaseModel):
    model_config = ConfigDict(extra="allow")

    company_name: str
    website: str
    industry: str | None = None
    location: str | None = None
    company_size: str | None = None
    company_size_value: int | None = None
    company_size_source: str = "unknown"
    industry_raw: str | None = None
    source_url: str | None = None
    source_name: str | None = None
    source_identifier: str | None = None
    customer_facing: bool = False
    search_dependent: bool = False
    content_rich: bool = False
    active: bool = True
    geo_opportunity: str = "unknown"
    unsuitable: bool = False
    dead_website: bool = False

    @field_validator("company_name", "website")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class DiscoverySource(ABC):
    source_type: str

    def __init__(self, path: Path):
        self.path = path

    @abstractmethod
    def discover(self) -> Iterator[CompanyCandidate]: ...


class YCPublicDirectorySource(DiscoverySource):
    """Small, sequential adapter for YC's public, robots-allowed static pages."""

    source_type = "yc_public"

    def __init__(
        self,
        path: Path | None = None,
        *,
        directory_url: str = "https://www.ycombinator.com/companies/industry/consumer",
        limit: int = 25,
        delay_seconds: float = 1.0,
        timeout_seconds: float = 15,
        user_agent: str = "GEOResearchOutreachAgent/0.2 (academic research; no outreach)",
        client: httpx.Client | None = None,
        seen_profile_urls: set[str] | None = None,
    ):
        self.path = Path("yc-public")
        self.directory_url = directory_url
        self.limit = max(1, min(limit, 50))
        self.delay_seconds = delay_seconds
        self.seen_profile_urls = seen_profile_urls if seen_profile_urls is not None else set()
        self.skipped_seen_profiles = 0
        self.profile_requests_attempted = 0
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds, follow_redirects=True, headers={"User-Agent": user_agent}
        )

    def discover(self) -> Iterator[CompanyCandidate]:
        try:
            parsed = httpx.URL(self.directory_url)
            robots_url = str(parsed.copy_with(path="/robots.txt", query=None, fragment=None))
            robots_response = self.client.get(robots_url)
            robots_response.raise_for_status()
            robots = RobotFileParser()
            robots.set_url(robots_url)
            robots.parse(robots_response.text.splitlines())
            if not robots.can_fetch(self.client.headers.get("User-Agent", "*"), self.directory_url):
                raise ValueError(f"robots.txt disallows discovery URL: {self.directory_url}")
            time.sleep(self.delay_seconds)
            response = self.client.get(self.directory_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            links: list[str] = []
            for anchor in soup.select('a[href^="/companies/"]'):
                href = str(anchor.get("href", "")).split("?", 1)[0].rstrip("/")
                if href.count("/") == 2 and href not in links:
                    links.append(href)
                if len(links) >= min(250, self.limit * 5):
                    break
            yielded = 0
            for index, href in enumerate(links):
                if index:
                    time.sleep(self.delay_seconds)
                detail_url = urljoin(self.directory_url, href)
                if detail_url in self.seen_profile_urls:
                    self.skipped_seen_profiles += 1
                    continue
                self.seen_profile_urls.add(detail_url)
                self.profile_requests_attempted += 1
                try:
                    detail = self.client.get(detail_url)
                    detail.raise_for_status()
                    candidate = self._parse_detail(detail.text, detail_url, directory_url=self.directory_url)
                except httpx.HTTPError as exc:
                    logger.warning("stage=discovery source=yc_public url=%s error=%s", detail_url, exc)
                    continue
                if candidate is not None:
                    yield candidate
                    yielded += 1
                    if yielded >= self.limit:
                        break
        finally:
            if self._owns_client:
                self.client.close()

    @staticmethod
    def _parse_detail(markup: str, detail_url: str, *, directory_url: str | None = None) -> CompanyCandidate | None:
        soup = BeautifulSoup(markup, "html.parser")
        page_node = soup.select_one('[data-page*="company"]')
        company: dict[str, Any] | None = None
        if page_node and page_node.get("data-page"):
            try:
                payload = json.loads(html.unescape(str(page_node["data-page"])))
                company = payload.get("props", {}).get("company")
            except (json.JSONDecodeError, TypeError):
                company = None
        if not company or not company.get("website") or not company.get("name"):
            return None
        team_size = company.get("team_size")
        if company.get("ycdc_status") != "Active" or (team_size is not None and int(team_size) > 250):
            return None
        size = "startup"
        return CompanyCandidate(
            company_name=company["name"],
            website=company["website"],
            industry=(company.get("tags") or [None])[0],
            industry_raw=(company.get("tags") or [None])[0],
            location=company.get("location"),
            company_size=size,
            company_size_value=int(team_size) if team_size is not None else None,
            company_size_source="directory_reported" if team_size is not None else "unknown",
            source_name="Y Combinator Public Startup Directory",
            source_url=detail_url,
            source_identifier=str(company.get("id") or company.get("slug")),
            source_directory_url=directory_url or f"https://www.ycombinator.com/companies/industry/consumer",
            active=company.get("ycdc_status") == "Active",
            # Directory membership alone is not website evidence for these signals.
            customer_facing=False,
            search_dependent=False,
            geo_opportunity="unknown",
        )


class GrowthZoneDirectorySource(DiscoverySource):
    """Bounded adapter for public GrowthZone chamber listing cards."""

    source_type = "growthzone_public"

    def __init__(
        self,
        path: Path | None = None,
        *,
        directory_url: str = "https://business.daltonchamber.org/list/",
        source_name: str = "Greater Dalton Chamber Member Directory",
        limit: int = 40,
        delay_seconds: float = 1.0,
        timeout_seconds: float = 15,
        user_agent: str = "GEOResearchOutreachAgent/0.3 (academic research; no outreach)",
        client: httpx.Client | None = None,
    ):
        self.path = Path("growthzone-public")
        self.directory_url = directory_url.rstrip("/") + "/"
        self.source_name = source_name
        self.limit = max(1, min(limit, 60))
        self.delay_seconds = delay_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers={"User-Agent": user_agent})

    def discover(self) -> Iterator[CompanyCandidate]:
        try:
            robots_url = urljoin(self.directory_url, "/robots.txt")
            robots_response = self.client.get(robots_url)
            robots_response.raise_for_status()
            robots = RobotFileParser(robots_url)
            robots.parse(robots_response.text.splitlines())
            if "/list/Search/" in self.directory_url:
                if not robots.can_fetch(self.client.headers.get("User-Agent", "*"), self.directory_url):
                    raise ValueError(f"robots.txt disallows discovery URL: {self.directory_url}")
                time.sleep(self.delay_seconds)
                response = self.client.get(self.directory_url)
                response.raise_for_status()
                for index, candidate in enumerate(self._parse_list(response.text, self.directory_url)):
                    yield candidate
                    if index + 1 >= self.limit:
                        break
                return
            yielded = 0
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                page_url = urljoin(self.directory_url, f"FindStartsWith?term={letter}")
                if not robots.can_fetch(self.client.headers.get("User-Agent", "*"), page_url):
                    raise ValueError(f"robots.txt disallows discovery URL: {page_url}")
                if yielded or letter != "A":
                    time.sleep(self.delay_seconds)
                response = self.client.get(page_url)
                response.raise_for_status()
                for candidate in self._parse_list(response.text, page_url):
                    yield candidate
                    yielded += 1
                    if yielded >= self.limit:
                        return
        finally:
            if self._owns_client:
                self.client.close()

    def _parse_list(self, markup: str, page_url: str) -> Iterator[CompanyCandidate]:
        soup = BeautifulSoup(markup, "html.parser")
        for card in soup.select(".gz-list-card, .gz-directory-card, [itemscope][itemtype*='Organization'], [itemscope][itemtype*='LocalBusiness']"):
            name_node = card.select_one("[itemprop='name']")
            website_node = card.select_one("a[href] [itemprop='sameAs']")
            website_anchor = website_node.parent if website_node and website_node.parent else next((
                anchor for anchor in card.select("a[href]")
                if anchor.get_text(" ", strip=True).casefold() in {"visit website", "website"}
            ), None)
            if not name_node or not website_anchor:
                continue
            website = urljoin(page_url, str(website_anchor.get("href", "")))
            if not website.startswith(("http://", "https://")) or "daltonchamber.org" in httpx.URL(website).host:
                continue
            detail_node = card.select_one(".gz-card-title a[href], a[itemprop='url']")
            detail_url = urljoin(page_url, str(detail_node.get("href"))) if detail_node else page_url
            identifier = detail_url.rstrip("/").rsplit("-", 1)[-1]
            locality = card.select_one("[itemprop='addressLocality']")
            region = card.select_one("[itemprop='addressRegion']")
            location = ", ".join(part.get_text(" ", strip=True) for part in (locality, region) if part)
            category_node = card.select_one(".gz-card-cat, .gz-card-category, [itemprop='category']")
            category = category_node.get_text(" ", strip=True) if category_node else None
            yield CompanyCandidate(
                company_name=name_node.get_text(" ", strip=True), website=website,
                industry=category, industry_raw=category, location=location or None,
                company_size="unknown", company_size_source="unknown",
                source_name=self.source_name, source_url=detail_url, source_identifier=identifier,
                source_directory_url=page_url, active=True, geo_opportunity="unknown",
            )


class CSVDiscoverySource(DiscoverySource):
    source_type = "csv"

    def discover(self) -> Iterator[CompanyCandidate]:
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                cleaned = {key: value for key, value in row.items() if value not in {None, ""}}
                yield CompanyCandidate.model_validate(cleaned)


class JSONDiscoverySource(DiscoverySource):
    source_type = "json"

    def discover(self) -> Iterator[CompanyCandidate]:
        with self.path.open(encoding="utf-8") as handle:
            data: Any = json.load(handle)
        if not isinstance(data, list):
            raise ValueError("JSON discovery input must be a list")
        for item in data:
            yield CompanyCandidate.model_validate(item)


def source_for(path: Path) -> DiscoverySource:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return CSVDiscoverySource(path)
    if suffix == ".json":
        return JSONDiscoverySource(path)
    raise ValueError("Discovery source must be a .csv or .json file")
