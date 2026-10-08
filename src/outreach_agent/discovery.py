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
    ):
        self.path = Path("yc-public")
        self.directory_url = directory_url
        self.limit = max(1, min(limit, 50))
        self.delay_seconds = delay_seconds
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds, follow_redirects=True, headers={"User-Agent": user_agent}
        )

    def discover(self) -> Iterator[CompanyCandidate]:
        try:
            parsed = httpx.URL(self.directory_url)
            robots_url = str(parsed.copy_with(path="/robots.txt", query=None, fragment=None))
            try:
                robots_response = self.client.get(robots_url)
                if robots_response.status_code == 200:
                    robots = RobotFileParser()
                    robots.set_url(robots_url)
                    robots.parse(robots_response.text.splitlines())
                    if not robots.can_fetch(self.client.headers.get("User-Agent", "*"), self.directory_url):
                        raise ValueError(f"robots.txt disallows discovery URL: {self.directory_url}")
                time.sleep(self.delay_seconds)
            except httpx.HTTPError as exc:
                logger.warning("stage=discovery source=yc_public robots_check_failed=%s", exc)
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
                try:
                    detail = self.client.get(detail_url)
                    detail.raise_for_status()
                    candidate = self._parse_detail(detail.text, detail_url)
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
    def _parse_detail(markup: str, detail_url: str) -> CompanyCandidate | None:
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
            location=company.get("location"),
            company_size=size,
            source_name="Y Combinator Consumer Startup Directory",
            source_url=detail_url,
            source_identifier=str(company.get("id") or company.get("slug")),
            active=company.get("ycdc_status") == "Active",
            # Directory membership alone is not website evidence for these signals.
            customer_facing=False,
            search_dependent=False,
            geo_opportunity="unknown",
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
