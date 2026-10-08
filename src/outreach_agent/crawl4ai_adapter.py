from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from outreach_agent.config import Crawl4AIFallbackSettings

ROLE_RE = re.compile(r"\b(founder|owner|chief|ceo|president|director|manager|marketing|growth|operations|executive|cfo|vp)\b", re.I)
NAME_RE = r"([A-Z][A-Za-z'’-]{1,30}\s+[A-Z][A-Za-z'’-]{1,30})"
BANNED_NAME_WORDS = {
    "about", "accessibility", "builders", "committee", "contact", "content", "cookie", "coaching",
    "details", "developer", "director", "donate", "email", "executive", "frequently", "github",
    "http", "leadership", "marketing", "maximum", "message", "platforms", "questions", "show",
    "staff", "statement", "storage", "subscribe", "team", "veeza", "who", "your",
}


@dataclass
class FallbackResult:
    status: str
    pages_processed: int = 0
    runtime_seconds: float = 0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    error: str | None = None


def _clean_line(line: str) -> str:
    line = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"^[#>*\-\s]+", "", line)
    return " ".join(line.replace("**", "").split()).strip()


def _plausible_name(value: str) -> bool:
    words = value.strip().split()
    return (
        len(words) == 2 and len(value) <= 60 and "@" not in value
        and not any(word.casefold().strip(".,:’'") in BANNED_NAME_WORDS for word in words)
    )


def _role_label(value: str) -> str:
    match = re.search(
        r"\b(?:co-?founder|founder|owner|chief executive officer|CEO|president|executive director|"
        r"marketing director|marketing manager|growth manager|operations director|director|manager|CFO|VP)\b",
        value, re.I,
    )
    return match.group(0) if match else value[:255]


def extract_markdown_people(markdown: str, source_url: str) -> tuple[list[dict[str, Any]], int]:
    """Extract narrow, evidenced name/role pairs from structure-preserving Markdown."""
    lines = [_clean_line(line) for line in markdown.splitlines()]
    lines = [line for line in lines if line]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected = 0
    for index, line in enumerate(lines):
        window = lines[max(0, index - 2): min(len(lines), index + 3)]
        pairs: list[tuple[str, str | None]] = []
        exact = line.strip(" .,:;|")
        if _plausible_name(exact):
            neighbors = [lines[p] for offset in (1, -1, 2, -2) if 0 <= (p := index + offset) < len(lines)]
            role = next((item for item in neighbors if ROLE_RE.search(item) and len(item) <= 120), None)
            if role:
                pairs.append((exact, role))
        patterns = (
            (rf"^{NAME_RE},\s*((?:co-?)?founder\b[^|.]*)", True),
            (rf"\b(?:our\s+)?founder,?\s+{NAME_RE}", False),
            (rf"\bI(?:'|’)m\s+{NAME_RE}", False),
        )
        for pattern, has_title in patterns:
            match = re.search(pattern, line, re.I)
            if match:
                wider = lines[max(0, index - 8): min(len(lines), index + 9)]
                role = match.group(2) if has_title else next((item for item in wider if ROLE_RE.search(item)), None)
                pairs.append((match.group(1), role))
        for name, title in pairs:
            normalized = name.casefold()
            if not _plausible_name(name) or not title or not ROLE_RE.search(title) or normalized in seen:
                rejected += 1
                continue
            seen.add(normalized)
            candidates.append({
                "name": name, "title": _role_label(title), "email": None, "contact_type": "PERSON",
                "source_url": source_url, "evidence": " | ".join(window)[:1000],
                "extraction_method": "CRAWL4AI_MARKDOWN", "confidence": "HIGH",
            })
    return candidates, rejected


class Crawl4AIAdapter:
    """Optional bounded renderer. Importing the production package never requires Crawl4AI."""

    def __init__(self, settings: Crawl4AIFallbackSettings, user_agent: str):
        self.settings = settings
        self.user_agent = user_agent

    @staticmethod
    def available() -> bool:
        try:
            import crawl4ai  # noqa: F401
        except ImportError:
            return False
        return True

    def extract(self, urls: list[str]) -> FallbackResult:
        if not self.available():
            return FallbackResult(status="UNAVAILABLE", error="Install the optional 'crawl4ai' dependency")
        try:
            return asyncio.run(self._extract(urls[: self.settings.max_pages_per_company]))
        except TimeoutError as exc:
            return FallbackResult(status="TIMEOUT", error=str(exc))
        except Exception as exc:  # optional browser failure must not stop enrichment
            return FallbackResult(status="FAILED", error=f"{type(exc).__name__}: {exc}")

    async def _extract(self, urls: list[str]) -> FallbackResult:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        started = time.perf_counter()
        candidates: list[dict[str, Any]] = []
        rejected = 0
        processed = 0
        config = CrawlerRunConfig(
            cache_mode=CacheMode.ENABLED, check_robots_txt=True,
            page_timeout=int(self.settings.timeout_seconds * 1000), scan_full_page=False,
            delay_before_return_html=0.25, verbose=False,
        )
        browser = BrowserConfig(headless=True, user_agent=self.user_agent, enable_stealth=False, verbose=False)
        try:
            async with asyncio.timeout(self.settings.timeout_seconds * max(1, len(urls)) + 5):
                async with AsyncWebCrawler(config=browser) as crawler:
                    for url in urls:
                        result = await crawler.arun(url=url, config=config)
                        if not result.success:
                            continue
                        processed += 1
                        markdown = result.markdown.raw_markdown if hasattr(result.markdown, "raw_markdown") else str(result.markdown or "")
                        if not markdown.strip():
                            continue
                        found, discarded = extract_markdown_people(markdown, result.url or url)
                        candidates.extend(found)
                        rejected += discarded
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Crawl4AI fallback exceeded its bounded timeout") from exc
        return FallbackResult(
            status="SUCCESS" if processed else "FAILED", pages_processed=processed,
            runtime_seconds=time.perf_counter() - started, candidates=candidates, rejected=rejected,
            error=None if processed else "No fallback page completed successfully",
        )
