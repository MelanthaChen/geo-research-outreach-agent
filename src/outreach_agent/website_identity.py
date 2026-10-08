"""Conservative first-party identity diagnostics for saved website snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from outreach_agent.models import Company, WebsiteSnapshot
from outreach_agent.normalization import normalize_domain, normalize_company_name
from outreach_agent.wikidata import is_third_party_profile

IDENTITY_STATUSES = {
    "VERIFIED", "AMBIGUOUS_IDENTITY", "INSUFFICIENT_EVIDENCE", "FETCH_FAILED",
    "ACCESS_BLOCKED", "ROBOTS_DENIED", "THIRD_PARTY_URL", "REJECTED_MISMATCH",
}
GENERIC_NAME_TOKENS = {
    "the", "and", "for", "with", "company", "companies", "group", "holdings",
    "services", "service", "solutions", "solution", "technology", "technologies",
    "systems", "international", "global", "inc", "llc", "ltd", "limited", "corp",
    "corporation", "co", "company", "center", "centre", "of", "at", "by",
}
PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "domain for sale", "parked free",
    "this domain may be for sale", "sedo domain parking", "afternic", "parkingcrew",
)
REPURPOSED_MARKERS = (
    "togel", "judi bola", "online casino", "casino bonus", "sports betting",
)
GENERIC_TITLES = {"home", "homepage", "welcome", "index", "website coming soon"}


@dataclass(frozen=True)
class WebsiteIdentityResult:
    status: str
    reason: str
    identity_tokens: str
    usable_content: bool


def _tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in GENERIC_NAME_TOKENS
    }


def verify_company_website(company: Company, snapshot: WebsiteSnapshot | None) -> WebsiteIdentityResult:
    """Classify identity only from saved crawl evidence; never equate HTTP 200 with identity."""
    if is_third_party_profile(company.website):
        return WebsiteIdentityResult("THIRD_PARTY_URL", "The submitted URL is a third-party profile/directory, not a company-controlled website", "", False)
    if snapshot is None:
        return WebsiteIdentityResult("FETCH_FAILED", "No website snapshot was captured", "", False)
    if snapshot.fetch_status == "ROBOTS_DENIED":
        return WebsiteIdentityResult("ROBOTS_DENIED", "robots.txt disallowed collection; no bypass attempted", "", False)
    if snapshot.fetch_status == "BLOCKED" or snapshot.http_status in {401, 403, 429}:
        return WebsiteIdentityResult("ACCESS_BLOCKED", "The site denied or rate-limited access; this is not evidence of an identity mismatch", "", False)
    successful_pages = [page for page in snapshot.pages if page.fetch_status in {"SUCCESS", "PARTIAL"}]
    if snapshot.fetch_status in {"FETCH_FAILED", "FAILED"} or not successful_pages:
        detail = snapshot.error_message or snapshot.fetch_status
        return WebsiteIdentityResult("FETCH_FAILED", f"No usable HTML was captured ({detail})", "", False)

    homepage = successful_pages[0]
    requested_domain = normalize_domain(company.website)
    final_url = homepage.final_url or snapshot.final_url or company.website
    final_domain = normalize_domain(final_url)
    name = normalize_company_name(company.company_name)
    name_tokens = _tokens(name)
    host = (urlsplit(final_url).hostname or "").casefold().removeprefix("www.")
    host_root = host.split(".")[0]
    host_tokens = _tokens(re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", " ", host_root))
    content = " ".join(
        [page.title or "" for page in successful_pages]
        + [page.meta_description or "" for page in successful_pages]
        + [(page.visible_text or "")[:20_000] for page in successful_pages]
    ).casefold()
    content_tokens = _tokens(content)
    domain_hits = name_tokens & host_tokens
    content_hits = name_tokens & content_tokens
    compact_name = re.sub(r"[^a-z0-9]", "", name)
    compact_host = re.sub(r"[^a-z0-9]", "", host_root)
    exact_brand_domain = len(compact_name) >= 4 and (compact_name in compact_host or compact_host in compact_name)
    hit_list = sorted(domain_hits | content_hits)
    usable_content = any(page.visible_text_length >= 500 for page in successful_pages)

    if any(marker in content for marker in PARKED_MARKERS):
        return WebsiteIdentityResult("REJECTED_MISMATCH", "Captured page contains explicit domain-sale/parking indicators", ";".join(hit_list), usable_content)
    if any(marker in content for marker in REPURPOSED_MARKERS) and not content_hits:
        return WebsiteIdentityResult("REJECTED_MISMATCH", "Captured content is repurposed/spam-like and contains no company-name evidence", ";".join(hit_list), usable_content)

    title_content = " ".join(page.title or "" for page in successful_pages).casefold()
    title_tokens = _tokens(title_content)
    title_hits = name_tokens & title_tokens
    compact_title = re.sub(r"[^a-z0-9]", "", title_content)
    title_exact = len(compact_name) >= 4 and compact_name in compact_title
    identity_supported = (
        exact_brand_domain and (title_exact or bool(title_hits))
        or len(name_tokens) >= 2 and bool(domain_hits) and (len(content_hits) >= 2 or bool(title_hits and domain_hits - title_hits))
        or len(name_tokens) == 1 and len(next(iter(name_tokens), "")) >= 5 and exact_brand_domain and (title_exact or bool(content_hits))
    )
    if identity_supported:
        return WebsiteIdentityResult("VERIFIED", "Company name/brand aligns with the website domain and captured first-party page title/content", ";".join(hit_list), usable_content)

    cross_domain_redirect = requested_domain != final_domain
    if cross_domain_redirect and not (content_hits or domain_hits):
        return WebsiteIdentityResult("REJECTED_MISMATCH", f"Redirected from {requested_domain} to unrelated {final_domain} without company-name evidence", ";".join(hit_list), usable_content)
    page_text_length = sum(page.visible_text_length for page in successful_pages)
    title = (homepage.title or "").strip().casefold()
    if page_text_length < 100 or not title or title in GENERIC_TITLES:
        return WebsiteIdentityResult("INSUFFICIENT_EVIDENCE", "The reachable page has too little distinctive identity evidence", ";".join(hit_list), usable_content)
    return WebsiteIdentityResult("AMBIGUOUS_IDENTITY", "The site returned content, but saved name/domain/page evidence does not securely link it to the company", ";".join(hit_list), usable_content)
