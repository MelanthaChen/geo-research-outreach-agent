from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from outreach_agent.contacts import extract_contact_evidence, normalize_email, normalize_role

ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).parent / "results"
USER_AGENT = "GEOResearchOutreachAgent-Crawl4AIEvaluation/0.1 (academic research; no outreach)"

SAMPLE = [
    ("Vyra", "yc_public", "https://www.usevyra.com/about"),
    ("Light Anchor", "yc_public", "https://lightanchor.ai/about"),
    ("Honeylove", "yc_public", "https://www.honeylove.com/pages/accessibility"),
    ("Alrol of America, Inc.", "growthzone_public", "https://alrolofamerica.com/about/"),
    ("Shire Intelligence", "yc_public", "https://shireintelligence.com/contact-us"),
    ("Veeza AI", "yc_public", "https://www.veeza.ai/en/about"),
    ("Jcode", "yc_public", "https://jcode.sh/about"),
    ("Bench Builders", "growthzone_public", "https://bench-builders.com/contact"),
    ("Big Brothers Big Sisters of Northwest Georgia Mountains", "growthzone_public", "https://bbbsngm.org/our-staff/"),
    ("Bruster's Real Ice Cream", "growthzone_public", "https://brusters.com/locations/dalton/349/"),
]

NAME_RE = re.compile(r"\b([A-Z][A-Za-z'’-]{1,30}\s+[A-Z][A-Za-z'’-]{1,30}(?:\s+[A-Z][A-Za-z'’-]{1,30})?)\b")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
ROLE_WORDS = re.compile(r"\b(founder|owner|chief|ceo|president|director|manager|marketing|growth|operations|executive|cfo|vp)\b", re.I)
BANNED = {"about", "accessibility", "and", "builders", "committee", "contact", "content", "cookie", "coaching", "details", "developer", "director", "donate", "email", "executive", "frequently", "github", "http", "leadership", "marketing", "maximum", "message", "platforms", "questions", "show", "staff", "statement", "storage", "subscribe", "team", "veeza", "who", "your"}
GENERIC = {"accessibility", "admin", "billing", "contact", "founders", "hello", "info", "legal", "marketing", "privacy", "support", "team"}


@dataclass(frozen=True)
class Contact:
    name: str | None
    title: str | None
    normalized_role: str
    email: str | None
    contact_type: str
    evidence_text: str
    evidence_url: str
    extraction_method: str
    confidence: str


def plausible_name(value: str) -> bool:
    words = value.strip().split()
    return 2 <= len(words) <= 3 and len(value) <= 60 and "@" not in value and not any(word.casefold().strip(".,:’'") in BANNED for word in words)


def clean_markdown_line(line: str) -> str:
    line = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"^[#>*\-\s]+", "", line)
    return " ".join(line.replace("**", "").split()).strip()


def crawl4ai_deterministic(markup: str, markdown: str, url: str) -> list[Contact]:
    """Generic evidence rules over Crawl4AI output; no per-company selectors."""
    soup = BeautifulSoup(markup, "html.parser")
    contacts: list[Contact] = []
    seen: set[tuple[str | None, str | None]] = set()

    # Preserve published mailboxes as generic unless a nearby personal address
    # and explicit name agree. Shared mailboxes are never assigned to a person.
    lines = [clean_markdown_line(line) for line in markdown.splitlines()]
    lines = [line for line in lines if line]
    for email_value in sorted(set(EMAIL_RE.findall(markdown + " " + soup.get_text(" ", strip=True)))):
        email = normalize_email(email_value)
        if not email:
            continue
        local = email.split("@", 1)[0]
        contacts.append(Contact(None, None, "FOUNDER_OWNER" if local == "founders" else "UNKNOWN", email,
                                "GENERIC_BUSINESS_CONTACT", email, url, "CRAWL4AI_DETERMINISTIC", "MEDIUM"))
        seen.add((None, email))

        # A personal local-part may be associated only with an exact nearby
        # full-name line whose first name agrees with that local-part.
        email_indexes = [i for i, line in enumerate(lines) if email.casefold() in line.casefold()]
        for email_index in email_indexes:
            for candidate_line in lines[max(0, email_index - 3): min(len(lines), email_index + 4)]:
                candidate = candidate_line.strip(" .,:;|")
                if plausible_name(candidate) and local.startswith(candidate.split()[0].casefold()):
                    contacts.append(Contact(candidate, None, "UNKNOWN", email, "PERSON", " | ".join(lines[max(0, email_index - 2):email_index + 2])[:500], url,
                                            "CRAWL4AI_DETERMINISTIC", "HIGH"))
                    seen.add((candidate.casefold(), email))
                    break

    # Crawl4AI Markdown retains useful line and heading boundaries. Use narrow,
    # explicit patterns rather than treating every capitalized phrase as a name.
    for index, line in enumerate(lines):
        window_lines = lines[max(0, index - 2): min(len(lines), index + 3)]
        window = " | ".join(window_lines)
        candidates: list[tuple[str, str | None]] = []
        exact = line.strip(" .,:;|")
        if plausible_name(exact):
            ordered_neighbors = [
                lines[position] for offset in (1, -1, 2, -2)
                if 0 <= (position := index + offset) < len(lines)
            ]
            role = next((part for part in ordered_neighbors if ROLE_WORDS.search(part) and len(part) <= 120), None)
            if role:
                candidates.append((exact, role))
        for pattern in (
            r"^([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+),\s*((?:co-?)?founder\b[^|.]*)",
            r"\b(?:our\s+)?founder,?\s+([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+)",
            r"\bI(?:'|’)m\s+([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+)",
        ):
            match = re.search(pattern, line, re.I if "founder" in pattern else 0)
            if match:
                name = match.group(1)
                wider = lines[max(0, index - 8): min(len(lines), index + 9)]
                title = match.group(2) if match.lastindex and match.lastindex > 1 else next((part for part in wider if ROLE_WORDS.search(part)), None)
                candidates.append((name, title))
        image_match = re.search(r"^([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+),\s*((?:Co-)?Founder[^,]*)", line)
        if image_match:
            candidates.append((image_match.group(1), image_match.group(2)))
        for name, title in candidates:
            if not plausible_name(name) or not title or not ROLE_WORDS.search(title):
                continue
            key = (name.casefold(), None)
            if key not in seen:
                contacts.append(Contact(name, title, normalize_role(title), None, "PERSON", window[:500], url,
                                        "CRAWL4AI_DETERMINISTIC", "HIGH"))
                seen.add(key)

    return contacts


def baseline_contacts(markup: str, url: str) -> list[Contact]:
    rows = []
    for item in extract_contact_evidence(markup, url):
        rows.append(Contact(
            item.get("name"), item.get("title"), normalize_role(item.get("title")), normalize_email(item.get("email")),
            item.get("contact_type") or "GENERIC_BUSINESS_CONTACT", item.get("evidence") or "", url,
            "EXISTING_EXTRACTOR", "HIGH" if item.get("name") else "MEDIUM",
        ))
    return rows


async def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    records = []
    browser = BrowserConfig(headless=True, user_agent=USER_AGENT, verbose=False, enable_stealth=False)
    config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, check_robots_txt=True, page_timeout=30_000,
                              delay_before_return_html=0.5, scan_full_page=False, verbose=False)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as client:
        async with AsyncWebCrawler(config=browser) as crawler:
            for company, source, url in SAMPLE:
                started = time.perf_counter()
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    existing = baseline_contacts(response.text, str(response.url))
                    existing_status = "SUCCESS"
                    existing_error = None
                except Exception as exc:  # benchmark must retain failures
                    existing, existing_status, existing_error = [], "FAILED", f"{type(exc).__name__}: {exc}"
                existing_seconds = time.perf_counter() - started
                records.append({"company_name": company, "source": source, "source_url": url, "approach": "A_EXISTING",
                                "crawl_status": existing_status, "processing_seconds": round(existing_seconds, 3),
                                "contacts": [asdict(item) for item in existing], "error": existing_error})

                await asyncio.sleep(1.0)
                started = time.perf_counter()
                try:
                    result = await crawler.arun(url=url, config=config)
                    markdown = result.markdown.raw_markdown if hasattr(result.markdown, "raw_markdown") else str(result.markdown or "")
                    markup = result.cleaned_html or result.html or ""
                    contacts = crawl4ai_deterministic(markup, markdown, result.url or url) if result.success else []
                    status, error = ("SUCCESS", None) if result.success else ("FAILED", result.error_message)
                    lengths = {"markdown_chars": len(markdown), "cleaned_html_chars": len(markup)}
                except Exception as exc:
                    contacts, status, error, lengths = [], "FAILED", f"{type(exc).__name__}: {exc}", {}
                seconds = time.perf_counter() - started
                records.append({"company_name": company, "source": source, "source_url": url, "approach": "B_CRAWL4AI_DETERMINISTIC",
                                "crawl_status": status, "processing_seconds": round(seconds, 3),
                                "contacts": [asdict(item) for item in contacts], "error": error, **lengths})
                await asyncio.sleep(1.0)

    (RESULTS / "benchmark_results.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    fields = ["company_name", "source", "source_url", "approach", "crawl_status", "processing_seconds", "name", "title",
              "normalized_role", "email", "contact_type", "evidence_text", "evidence_url", "extraction_method", "confidence", "error"]
    with (RESULTS / "benchmark_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            contacts = record["contacts"] or [{}]
            for contact in contacts:
                writer.writerow({key: contact.get(key, record.get(key, "")) for key in fields})


if __name__ == "__main__":
    asyncio.run(main())
