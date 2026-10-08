from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach_agent.models import (
    BusinessChannelType, Company, Contact, ContactDiscoveryStatus, ContactEvidence,
    ContactExtractionRun, ContactType, ContactabilityStatus, ReviewStatus, WebsiteSnapshot,
)
from outreach_agent.config import Crawl4AIFallbackSettings
from outreach_agent.crawl4ai_adapter import Crawl4AIAdapter

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
EMAIL_OBFUSCATION_RE = re.compile(r"\b([A-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|&#64;|\bat\b)\s*([A-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|&#46;|\bdot\b)\s*([A-Z]{2,})\b", re.I)
GENERIC_LOCAL_PARTS = {
    "accessibility", "admin", "billing", "business", "careers", "contact", "dpo", "founders",
    "hello", "info", "legal", "marketing", "office", "partners", "partnerships", "privacy",
    "sales", "support", "team", "webmaster",
}
ROLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("FOUNDER_OWNER", ("founder", "co-founder", "cofounder", "owner", "proprietor")),
    ("MARKETING_GROWTH", ("marketing", "growth", "demand generation", "brand")),
    ("SEO_CONTENT", ("seo", "content", "editorial")),
    ("PARTNERSHIPS", ("partnership", "alliances")),
    ("BUSINESS_DEVELOPMENT", ("business development", "biz dev")),
    ("DIGITAL_ECOMMERCE", ("digital", "ecommerce", "e-commerce")),
    ("COMMUNICATIONS", ("communications", "public relations", "media relations")),
    ("EXECUTIVE", ("chief executive", "ceo", "president", "managing director", "general manager", "principal")),
    ("GENERAL_BUSINESS", ("manager", "director", "business", "office")),
)


@dataclass(frozen=True)
class ContactCandidate:
    name: str | None
    title: str | None
    email: str | None
    contact_type: str
    source_url: str
    evidence: str
    extraction_method: str = "LIGHTWEIGHT_HTML"
    confidence: str = "HIGH"


@dataclass
class EnrichmentSummary:
    processed: int = 0
    found: int = 0
    not_found: int = 0
    blocked: int = 0
    failed: int = 0
    contacts_created: int = 0
    duplicates: int = 0
    fallback_triggered: int = 0
    fallback_succeeded: int = 0
    fallback_failed: int = 0
    fallback_pages: int = 0
    fallback_contacts_created: int = 0
    fallback_evidence_rejected: int = 0
    fallback_runtime_seconds: float = 0


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip(".,;:<>[]()\"'").casefold()
    return value if EMAIL_RE.fullmatch(value) else None


def normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    result = " ".join(re.sub(r"[^\w' -]", " ", value, flags=re.UNICODE).casefold().split())
    return result or None


def normalize_role(title: str | None) -> str:
    folded = " ".join((title or "").casefold().replace("&", " and ").split())
    for role, terms in ROLE_RULES:
        if any(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", folded) for term in terms):
            return role
    return "OTHER" if folded else "UNKNOWN"


def _plausible_name(value: str) -> bool:
    words = value.strip().split()
    folded = value.casefold().strip(" .:-")
    banned_terms = {
        "about", "apply", "committee", "contact", "content", "copyright", "executive", "faq",
        "frequently", "functions", "leadership", "management", "our", "questions", "statement",
        "age", "agreement", "ai", "apple", "arbitration", "banker", "board", "business", "care",
        "class", "coldwell", "company", "control", "customer", "data", "disclaimer", "elect", "export",
        "joint", "law", "notice", "opt-out", "past", "platform", "privacy", "realtor", "references",
        "requirements", "reseller", "rules", "service", "startup", "team", "tech", "termination",
        "terms", "use", "vice", "waiver", "works", "your",
    }
    return (
        2 <= len(words) <= 3 and len(value) <= 60
        and bool(re.fullmatch(r"[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,2}", value.strip()))
        and not any(term in banned_terms for term in folded.split())
        and all(any(ch.isalpha() for ch in word) and (word[0].isupper() or word.isupper()) for word in words)
    )


def _nearby_person_text(text: str, email: str) -> str | None:
    """Require an email local-part to agree with a nearby explicit full name."""
    local = email.split("@", 1)[0].split("+", 1)[0].casefold()
    if local in GENERIC_LOCAL_PARTS:
        return None
    text = " ".join(text.split())[:1200]
    name_pattern = re.compile(r"(?=(\b[A-Z][A-Za-z'’-]{1,30}\s+[A-Z][A-Za-z'’-]{1,30}(?:\s+[A-Z][A-Za-z'’-]{1,30})?)\b)")
    names = [match.group(1) for match in name_pattern.finditer(text)]
    def local_matches(candidate: str) -> bool:
        first = normalize_name(candidate).split()[0]
        words = normalize_name(candidate).split()
        joined = "".join(words)
        first_initial_last = words[0][0] + words[-1] if len(words) >= 2 else ""
        return (
            local == first or any(local.startswith(first + separator) for separator in (".", "_", "-"))
            or local == joined or local.startswith(joined)
            or bool(first_initial_last and local.startswith(first_initial_last))
        )
    return next((candidate for candidate in names if _plausible_name(candidate) and local_matches(candidate)), None)


def _nearby_person(container, email: str) -> tuple[str | None, str | None]:
    """Require a personal local-part to agree with a nearby explicit full name."""
    if not container:
        return None, None
    text = " ".join(container.get_text(" ", strip=True).split())[:1200]
    name = _nearby_person_text(text, email)
    if not name:
        return None, None
    short_nodes = [" ".join(node.get_text(" ", strip=True).split()) for node in container.select("h1,h2,h3,h4,h5,h6,strong,b")]
    title = next((item for item in short_nodes if len(item) <= 100 and normalize_role(item) not in {"UNKNOWN", "OTHER"}), None)
    return name, title


def _standalone_people(soup: BeautifulSoup, source_url: str) -> list[ContactCandidate]:
    results: list[ContactCandidate] = []
    excluded = re.compile(r"testimonial|customer|partner|sponsor|vendor|author|blog|press", re.I)
    nodes = soup.select("h1,h2,h3,h4,h5,h6,strong,b,[class~='name' i],[class$='-name' i],[class$='__name' i]")
    for node in nodes:
        ancestor = node.find_parent(["article", "li", "section", "div"])
        marker = " ".join(str(ancestor.get(key, "")) for key in ("id", "class")) if ancestor else ""
        if excluded.search(marker):
            continue
        name = " ".join(node.get_text(" ", strip=True).split())
        if not _plausible_name(name):
            continue
        neighbors = [node.find_previous_sibling(), node.find_next_sibling()]
        class_marker = " ".join(str(value) for value in node.get("class", []))
        if ancestor and re.search(r"(?:^|[-_])name(?:$|[-_])", class_marker, re.I):
            neighbors.extend(ancestor.find_all(["p", "strong", "b", "span"], recursive=False))
        texts = [" ".join(item.get_text(" ", strip=True).split()) for item in neighbors if item and item is not node]
        title = next((item for item in texts if len(item) <= 100 and normalize_role(item) not in {"UNKNOWN", "OTHER"}), None)
        if title:
            results.append(ContactCandidate(name, title, None, "PERSON", source_url, f"{name} — {title}"))
    for container in soup.select("[class*='testimonial' i],[class*='customer' i],[class*='vendor' i],[class*='author' i],[class*='blog' i]"):
        container.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    patterns = (
        rf"\b([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+),\s*((?:co-?)?founder|owner|CEO|president|executive director)\b",
        rf"\b([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+)\s+is\s+(?:the\s+|an?\s+)?((?:co-?)?founder|owner|CEO|president|executive director)\b",
        rf"\b(?:our\s+)?((?:co-?)?founder|owner|CEO|president),?\s+([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+)\b",
        rf"\b(?:Hi,?\s+)?I(?:'|’)m\s+([A-Z][A-Za-z'’-]+\s+[A-Z][A-Za-z'’-]+)\b(.{{0,180}}?\b(?:founder|owner|CEO|president)\b)",
    )
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text, re.I):
            name, title = ((match.group(2), match.group(1)) if index == 2 else (match.group(1), match.group(2)))
            if index == 3:
                role_match = re.search(r"(?:co-?)?founder|owner|CEO|president", title, re.I)
                title = role_match.group(0) if role_match else title
            if _plausible_name(name):
                evidence = text[max(0, match.start() - 80):match.end() + 80]
                results.append(ContactCandidate(name, title, None, "PERSON", source_url, evidence[:600]))
    return results


def extract_contact_evidence(markup: str, source_url: str) -> list[dict[str, str | None]]:
    """Extract only explicit public contact facts; never derive names or emails."""
    soup = BeautifulSoup(markup, "html.parser")
    candidates: list[ContactCandidate] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        queue = payload if isinstance(payload, list) else [payload]
        while queue:
            row = queue.pop()
            if isinstance(row, dict):
                queue.extend(value for value in row.values() if isinstance(value, (dict, list)))
                value = row.get("email")
                if isinstance(value, str):
                    email = normalize_email(value.removeprefix("mailto:"))
                    if email:
                        candidates.append(ContactCandidate(None, None, email, "GENERIC_BUSINESS_CONTACT", source_url, json.dumps(row)[:600]))
    for anchor in soup.select('a[href^="mailto:" i]'):
        email = normalize_email(str(anchor.get("href", "")).split(":", 1)[-1].split("?", 1)[0])
        if not email:
            continue
        containers = list(anchor.parents)[:5]
        container = next((item for item in containers if item.name in {"article", "li", "section", "div", "footer"}), anchor.parent)
        text = " ".join(container.get_text(" ", strip=True).split())[:600] if container else ""
        name, title = _nearby_person(container, email) if container else (None, None)
        kind = "PERSON" if name else "GENERIC_BUSINESS_CONTACT"
        key = (normalize_name(name), normalize_email(email), normalize_role(title))
        if key not in seen:
            seen.add(key)
            candidates.append(ContactCandidate(name, title, email, kind, source_url, text))
    # Visible addresses not wrapped in mailto remain explicit evidence, but are generic
    # unless a structured nearby person block already established identity.
    for email in EMAIL_RE.findall(soup.get_text(" ", strip=True)):
        email = normalize_email(email)
        key = (None, email, "UNKNOWN")
        if email and not any(candidate.email == email for candidate in candidates) and key not in seen:
            seen.add(key)
            candidates.append(ContactCandidate(None, None, email, "GENERIC_BUSINESS_CONTACT", source_url, email))
    for candidate in _standalone_people(soup, source_url):
        key = (normalize_name(candidate.name), None, normalize_role(candidate.title))
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)
    return [asdict(candidate) for candidate in candidates]


PURPOSE_RULES: tuple[tuple[BusinessChannelType, re.Pattern[str]], ...] = (
    (BusinessChannelType.RESTRICTED_EMAIL, re.compile(r"\b(?:privacy|accessibility|legal|abuse|security|data protection|dpo)\b", re.I)),
    (BusinessChannelType.SUPPORT_EMAIL, re.compile(r"\b(?:customer support|support|help desk|technical support)\b", re.I)),
    (BusinessChannelType.PARTNERSHIP_EMAIL, re.compile(r"\b(?:partnerships?|collaborations?|research collaboration|partner with us)\b", re.I)),
    (BusinessChannelType.BUSINESS_DEVELOPMENT_EMAIL, re.compile(r"\b(?:business inquiries|business development|business opportunities|business enquiries)\b", re.I)),
    (BusinessChannelType.SALES_MARKETING_EMAIL, re.compile(r"\b(?:sales|marketing|press|media inquiries|media relations)\b", re.I)),
    (BusinessChannelType.GENERAL_BUSINESS_EMAIL, re.compile(r"\b(?:general inquiries|general enquiries|contact us|contact information|reach us|email us)\b|/(?:contact|contact-us)(?:/|$)", re.I)),
)


def classify_channel(evidence: str, email: str | None = None, is_form: bool = False) -> BusinessChannelType:
    context = evidence or ""
    local = email.split("@", 1)[0].casefold() if email else ""
    if local in {"privacy", "accessibility", "legal", "abuse", "security", "dpo"}:
        return BusinessChannelType.RESTRICTED_EMAIL
    if is_form:
        if re.search(r"newsletter|subscribe|login|sign[- ]?in|password|job application|careers|apply now|privacy|accessibility|legal", context, re.I):
            return BusinessChannelType.OTHER
        if re.search(r"\b(?:partnership|collaboration|business inquiry|business enquiry|contact us|general inquiry|general enquiry)\b|/(?:contact|contact-us)(?:/|$)", context, re.I):
            return BusinessChannelType.CONTACT_FORM
        return BusinessChannelType.OTHER
    for channel_type, pattern in PURPOSE_RULES:
        if pattern.search(context):
            return channel_type
    # A mailbox's explicit role can support a channel class, but never infer
    # partnership intent from generic addresses such as info@ or hello@.
    if local in {"partnership", "partnerships", "collaboration", "collaborations"}:
        return BusinessChannelType.PARTNERSHIP_EMAIL
    if local in {"bizdev", "businessdevelopment"}:
        return BusinessChannelType.BUSINESS_DEVELOPMENT_EMAIL
    if local in {"sales", "marketing", "press", "media"}:
        return BusinessChannelType.SALES_MARKETING_EMAIL
    if local in {"info", "hello", "contact", "office", "team", "business", "founder", "founders"}:
        return BusinessChannelType.GENERAL_BUSINESS_EMAIL
    return BusinessChannelType.CONTACT_FORM if is_form else BusinessChannelType.OTHER


def _evidence_context(container, target: str, limit: int = 700) -> str:
    """Return a compact local text window around the explicit channel, not unrelated page text."""
    text = " ".join(container.get_text(" ", strip=True).split())
    position = text.casefold().find(target.casefold()) if target else -1
    if position < 0:
        return text[:limit]
    start = max(0, position - limit // 2)
    end = min(len(text), position + len(target) + limit // 2)
    return text[start:end]


def _channel_evidence(markup: str, page_url: str) -> list[dict[str, object]]:
    soup = BeautifulSoup(markup, "html.parser")
    rows: list[dict[str, object]] = []
    for anchor in soup.select('a[href^="mailto:" i]'):
        email = normalize_email(str(anchor.get("href", "")).split(":", 1)[-1].split("?", 1)[0])
        if not email:
            continue
        containers = [parent for parent in list(anchor.parents)[:6] if parent.name in {"article", "li", "section", "div", "footer"}]
        container = next((item for item in containers if len(item.get_text(" ", strip=True)) <= 500), anchor.parent)
        context = _evidence_context(container, email) if container else anchor.get_text(" ", strip=True)
        name, title = _nearby_person(container, email) if container else (None, None)
        previous = anchor.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
        if previous:
            context = (" ".join(previous.get_text(" ", strip=True).split()) + " | " + context)[:700]
        channel_type = BusinessChannelType.NAMED_PERSON_EMAIL if name else classify_channel(context, email)
        rows.append({"email": email, "name": name, "title": title, "type": channel_type, "url": page_url, "evidence": context or anchor.get_text(" ", strip=True), "purpose": context[:500]})
    visible = soup.get_text(" ", strip=True)
    for match in EMAIL_OBFUSCATION_RE.finditer(visible):
        email = normalize_email(f"{match.group(1)}@{match.group(2)}.{match.group(3)}")
        if email:
            context = match.group(0)
            purpose_context = (context + " " + page_url)[:700]
            rows.append({"email": email, "type": classify_channel(purpose_context, email), "url": page_url, "evidence": context, "purpose": context[:500]})
    for raw in EMAIL_RE.findall(visible):
        email = normalize_email(raw)
        if email and not any(row.get("email") == email for row in rows):
            around = re.search(rf".{{0,180}}{re.escape(raw)}.{{0,180}}", visible, re.I)
            context = around.group(0) if around else raw
            # Recover a nearby explicit person/email pair from the DOM where
            # possible (e.g. a founder email rendered as plain text).
            text_node = next((node for node in soup.find_all(string=True) if raw.casefold() in str(node).casefold()), None)
            container = text_node.parent if text_node else None
            for _ in range(4):
                if container and container.name in {"li", "article", "section", "footer", "div"}:
                    break
                container = container.parent if container else None
            dom_context = _evidence_context(container, email) if container else context
            name, title = _nearby_person(container, email) if container else (None, None)
            channel_type = BusinessChannelType.NAMED_PERSON_EMAIL if name else classify_channel(dom_context, email)
            # Avoid turning an arbitrary personal address into a business route.
            if channel_type != BusinessChannelType.OTHER:
                rows.append({"email": email, "name": name, "title": title, "type": channel_type,
                             "url": page_url, "evidence": dom_context, "purpose": dom_context[:500]})
    for script in soup.select('script[type="application/ld+json" i]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(value for value in item.values() if isinstance(value, (dict, list)))
                raw_email = item.get("email")
                email = normalize_email(str(raw_email).removeprefix("mailto:")) if raw_email else None
                if email:
                    context = json.dumps(item)[:1200]
                    channel_type = classify_channel(context, email)
                    json_type = item.get("@type", "")
                    is_business = json_type in {"Organization", "LocalBusiness", "MedicalOrganization", "Corporation", "Store"} or (
                        isinstance(json_type, list) and any(value in {"Organization", "LocalBusiness", "MedicalOrganization", "Corporation", "Store"} for value in json_type)
                    )
                    is_person = json_type == "Person" or (isinstance(json_type, list) and "Person" in json_type)
                    if is_person:
                        channel_type = BusinessChannelType.NAMED_PERSON_EMAIL
                    elif channel_type == BusinessChannelType.OTHER and is_business:
                        channel_type = BusinessChannelType.GENERAL_BUSINESS_EMAIL
                    # Keep source markup for review but do not treat the whole schema
                    # object (often just an address) as a human-readable purpose.
                    purpose = str(item.get("contactType") or item.get("description") or "")[:500]
                    rows.append({"email": email, "name": item.get("name") if is_person else None, "type": channel_type, "url": page_url, "evidence": context, "purpose": purpose})
            elif isinstance(item, list):
                stack.extend(item)
    for form in soup.find_all("form"):
        fields = " ".join(
            str(item.get("name", "")) + " " + str(item.get("id", "")) + " " + str(item.get("placeholder", ""))
            for item in form.select("input,textarea,select,button")
        ).casefold()
        label_context = " ".join(form.get_text(" ", strip=True).split())
        heading = form.find_previous(["h1", "h2", "h3", "h4"])
        context = " ".join(x for x in (heading.get_text(" ", strip=True) if heading else "", label_context, fields) if x)
        if re.search(r"newsletter|subscribe|login|sign[- ]?in|password|job application|careers|apply now|privacy|accessibility|legal", context, re.I):
            continue
        if not form.select_one('input[type="email" i],input[name*="email" i],textarea[name*="message" i]'):
            continue
        action = urljoin(page_url, str(form.get("action") or page_url))
        context = (context + " " + page_url)[:1800]
        form_type = classify_channel(context, is_form=True)
        if form_type != BusinessChannelType.OTHER:
            rows.append({"email": None, "type": form_type, "url": action, "source_url": page_url, "evidence": context[:1200], "purpose": context[:500], "form": True})
    for anchor in soup.select("a[href]"):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        href = urljoin(page_url, str(anchor.get("href")))
        if re.search(r"\b(?:contact us|contact|partnerships?|collaborations?|business inquiries|contact sales)\b", label, re.I) and urlparse(href).scheme in {"http", "https"}:
            destination = urlparse(href).path.casefold()
            if re.search(r"accessibility|privacy|legal|abuse|careers|jobs|login|sign-in|subscribe|newsletter", destination):
                continue
            if urlparse(href).netloc != urlparse(page_url).netloc:
                rows.append({"email": None, "type": BusinessChannelType.CONTACT_FORM, "url": href, "source_url": page_url, "evidence": label, "purpose": label, "form": True, "link": True})
    unique: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        unique[(row.get("email"), row["type"], row["url"])] = row
    return list(unique.values())


def _source_belongs_to_company(source_url: str, company_domain: str) -> bool:
    hostname = (urlparse(source_url).hostname or "").casefold().removeprefix("www.")
    domain = company_domain.casefold().removeprefix("www.")
    return hostname == domain or hostname.endswith("." + domain)


def _generic_mailbox_role(email: str | None) -> str:
    local = (email or "").split("@", 1)[0].casefold()
    if local in {"founder", "founders", "owner", "owners"}:
        return "FOUNDER_OWNER"
    if local in {"partnership", "partnerships", "partners"}:
        return "PARTNERSHIPS"
    if local in {"marketing", "growth"}:
        return "MARKETING_GROWTH"
    if local in {"business", "bizdev"}:
        return "BUSINESS_DEVELOPMENT"
    return "UNKNOWN"


def _site_matches_company(company: Company, snapshot: WebsiteSnapshot | None) -> bool:
    if not snapshot or not snapshot.pages:
        return True
    homepage = (snapshot.pages[0].visible_text or "").casefold()
    ignored = {"america", "company", "group", "inc", "llc", "responsive", "the"}
    tokens = [token for token in re.findall(r"[a-z0-9]+", company.company_name.casefold()) if len(token) >= 4 and token not in ignored]
    return not tokens or any(token in homepage for token in tokens)


def _context(company: Company) -> str:
    sources = " ".join(item.source_type.casefold() for item in company.discoveries)
    if "growthzone" in sources:
        return "local_smb"
    if company.company_size_category in {"MEDIUM", "LARGE"}:
        return "medium_company"
    return "startup_or_small"


WEIGHTS = {
    "startup_or_small": {"FOUNDER_OWNER": 100, "EXECUTIVE": 90, "MARKETING_GROWTH": 82, "SEO_CONTENT": 76, "PARTNERSHIPS": 72, "BUSINESS_DEVELOPMENT": 68, "DIGITAL_ECOMMERCE": 65, "COMMUNICATIONS": 62, "GENERAL_BUSINESS": 50, "OTHER": 30, "UNKNOWN": 20},
    "medium_company": {"MARKETING_GROWTH": 100, "DIGITAL_ECOMMERCE": 94, "SEO_CONTENT": 92, "PARTNERSHIPS": 86, "COMMUNICATIONS": 82, "BUSINESS_DEVELOPMENT": 78, "EXECUTIVE": 60, "FOUNDER_OWNER": 55, "GENERAL_BUSINESS": 45, "OTHER": 30, "UNKNOWN": 20},
    "local_smb": {"FOUNDER_OWNER": 100, "EXECUTIVE": 92, "MARKETING_GROWTH": 78, "GENERAL_BUSINESS": 74, "DIGITAL_ECOMMERCE": 70, "BUSINESS_DEVELOPMENT": 66, "PARTNERSHIPS": 62, "SEO_CONTENT": 58, "COMMUNICATIONS": 55, "OTHER": 30, "UNKNOWN": 20},
}


def rank_contact(company: Company, role: str, contact_type: ContactType, has_email: bool) -> tuple[float, str]:
    context = _context(company)
    score = float(WEIGHTS[context].get(role, 20))
    score += 8 if has_email else 0
    score -= 25 if contact_type == ContactType.GENERIC_BUSINESS_CONTACT else 0
    return max(score, 0), f"{context}: {role} role weight; {'explicit public email' if has_email else 'no public email'}; {contact_type.value.lower()}"


def _fallback_urls(snapshot: WebsiteSnapshot) -> list[str]:
    high_value = re.compile(r"(?:^|/)(?:about|team|leadership|staff|management|founders?)(?:/|$)|who-we-are", re.I)
    signals = re.compile(r"\b(?:our team|leadership|co-founder|founder|executive director)\b", re.I)
    pages = [page for page in snapshot.pages if page.fetch_status in {"SUCCESS", "PARTIAL"}]
    pages.sort(key=lambda page: (not bool(high_value.search(page.final_url or page.requested_url)), not bool(signals.search(page.visible_text or ""))))
    return list(dict.fromkeys(page.final_url or page.requested_url for page in pages if high_value.search(page.final_url or page.requested_url) or signals.search(page.visible_text or "")))


def _has_named_person(company: Company, evidence: list[dict[str, object]]) -> bool:
    return any(contact.contact_type == ContactType.PERSON and contact.normalized_name for contact in company.contacts) or any(
        item.get("contact_type") == "PERSON" and item.get("name") and _plausible_name(str(item["name"]))
        and (normalize_role(str(item.get("title") or "")) not in {"UNKNOWN", "OTHER"} or bool(normalize_email(str(item.get("email") or ""))))
        for item in evidence
    )


def _add_evidence(session: Session, contact: Contact, item: dict[str, object]) -> None:
    url = str(item.get("source_url") or contact.source_url or "")
    method = str(item.get("extraction_method") or "LIGHTWEIGHT_HTML")
    if not url:
        return
    exists = session.scalar(select(ContactEvidence).where(
        ContactEvidence.contact_id == contact.id, ContactEvidence.source_url == url,
        ContactEvidence.extraction_method == method,
    ))
    if not exists:
        session.add(ContactEvidence(
            contact_id=contact.id, source_url=url, extraction_method=method,
            evidence_text=str(item.get("evidence") or "")[:4000],
            confidence=str(item.get("confidence") or contact.confidence),
        ))


def _persist_candidate(session: Session, company: Company, item: dict[str, object]) -> tuple[bool, bool]:
    email = normalize_email(str(item.get("email") or ""))
    name = str(item.get("name") or "").strip() or None
    raw_kind = str(item.get("contact_type") or "GENERIC_BUSINESS_CONTACT")
    kind = ContactType(raw_kind) if raw_kind in {value.value for value in ContactType} else ContactType.GENERIC_BUSINESS_CONTACT
    source_url = str(item.get("source_url") or "")
    if kind == ContactType.PERSON and re.search(r"/(?:terms|privacy|legal|faq)(?:[/?#]|$)", source_url, re.I):
        return False, False
    if name and not _plausible_name(name):
        name, kind = None, ContactType.GENERIC_BUSINESS_CONTACT
    evidence_text = str(item.get("evidence") or "")
    if name and re.search(
        rf"\b(?:president|founder|owner|director|manager)\s+{re.escape(name)}\s+(?:[A-Z][A-Za-z&'’-]*\s+)?(?:capital|company|industries|realty|bank|group|llc|inc)\b",
        evidence_text, re.I,
    ):
        return False, False
    if name and email:
        local = email.split("@", 1)[0].casefold()
        first = normalize_name(name).split()[0]
        if local in GENERIC_LOCAL_PARTS or not (local == first or any(local.startswith(first + separator) for separator in (".", "_", "-"))):
            name, kind = None, ContactType.GENERIC_BUSINESS_CONTACT
    title = str(item.get("title") or "").strip() or None if name else None
    role = normalize_role(title)
    if kind == ContactType.PERSON and not email and role in {"UNKNOWN", "OTHER"}:
        return False, False
    if kind == ContactType.GENERIC_BUSINESS_CONTACT and role in {"UNKNOWN", "OTHER"}:
        role = _generic_mailbox_role(email)
    if not name and not email:
        return False, False
    existing = session.scalar(select(Contact).where(Contact.normalized_email == email)) if email else session.scalar(
        select(Contact).where(Contact.company_id == company.id, Contact.normalized_name == normalize_name(name))
    )
    if existing:
        _add_evidence(session, existing, item)
        return False, True
    score, reason = rank_contact(company, role, kind, bool(email))
    local = (email or "").split("@", 1)[0]
    unsuitable = local in {"accessibility", "billing", "careers", "dpo", "legal", "privacy", "support", "webmaster"}
    if unsuitable:
        score, reason = max(0, score - 20), reason + "; low-suitability mailbox"
    email_domain = (email or "@").split("@", 1)[-1]
    validation = "NO_EMAIL" if not email else ("SYNTAX_VALID" if email_domain == company.normalized_domain or email_domain.endswith("." + company.normalized_domain) else "PUBLIC_EXTERNAL_DOMAIN")
    method = str(item.get("extraction_method") or "LIGHTWEIGHT_HTML")
    confidence = str(item.get("confidence") or ("HIGH" if name and (email or role != "UNKNOWN") else ("LOW" if unsuitable else "MEDIUM")))
    contact = Contact(
        company_id=company.id, name=name, normalized_name=normalize_name(name), title=title,
        normalized_role=role, email=email, normalized_email=email, contact_type=kind,
        source_type="FIRST_PARTY_WEBSITE", source_url=source_url or None,
        extraction_method=method, evidence_text=evidence_text[:4000],
        validation_status=validation, confidence=confidence, ranking_score=score, ranking_reason=reason,
    )
    contact.company = company
    session.add(contact)
    session.flush()
    _add_evidence(session, contact, item)
    return True, False


CHANNEL_RANK = {
    BusinessChannelType.PARTNERSHIP_EMAIL: 100,
    BusinessChannelType.BUSINESS_DEVELOPMENT_EMAIL: 90,
    BusinessChannelType.GENERAL_BUSINESS_EMAIL: 80,
    BusinessChannelType.CONTACT_FORM: 70,
    BusinessChannelType.NAMED_PERSON_EMAIL: 60,
    BusinessChannelType.SALES_MARKETING_EMAIL: 50,
    BusinessChannelType.OTHER: 10,
    BusinessChannelType.SUPPORT_EMAIL: 0,
    BusinessChannelType.RESTRICTED_EMAIL: -10,
}


def _channel_contact_type(channel: BusinessChannelType) -> ContactType:
    return ContactType.PERSON if channel == BusinessChannelType.NAMED_PERSON_EMAIL else ContactType.GENERIC_BUSINESS_CONTACT


def _store_channel(session: Session, company: Company, item: dict[str, object]) -> tuple[int, bool]:
    channel_type = item["type"]
    if not isinstance(channel_type, BusinessChannelType):
        channel_type = BusinessChannelType(channel_type)
    email = normalize_email(str(item.get("email") or ""))
    target = str(item.get("url") or "")
    source = str(item.get("source_url") or target)
    evidence = str(item.get("evidence") or "")[:4000]
    contact = session.scalar(select(Contact).where(Contact.normalized_email == email)) if email else session.scalar(select(Contact).where(
        Contact.company_id == company.id, Contact.channel_type == channel_type, Contact.channel_url == target,
    ))
    if contact:
        if contact.company_id != company.id:
            return contact.id, False
        old_rank = CHANNEL_RANK.get(contact.channel_type, -100)
        if contact.channel_type is None or CHANNEL_RANK[channel_type] > CHANNEL_RANK.get(contact.channel_type, -100):
            contact.channel_type = channel_type
        if not contact.purpose or CHANNEL_RANK[channel_type] > old_rank:
            contact.purpose = str(item.get("purpose") or "")[:1000]
        if item.get("form") and not contact.channel_url:
            contact.channel_url = target
        if item.get("name") and not contact.name:
            contact.name = str(item["name"])
            contact.normalized_name = normalize_name(contact.name)
            contact.title = str(item.get("title") or "") or None
            contact.normalized_role = normalize_role(contact.title)
            contact.contact_type = ContactType.PERSON
        if not contact.evidence_text:
            contact.evidence_text = evidence
        if not contact.source_url:
            contact.source_url = source
        # Merge additional evidence without disturbing human review fields.
        _add_evidence(session, contact, {"source_url": source, "extraction_method": "CHANNEL_DISCOVERY", "evidence": evidence, "confidence": contact.confidence})
        return contact.id, False
    is_form = bool(item.get("form"))
    named_match = next((person for person in company.contacts if item.get("name") and person.normalized_name == normalize_name(str(item["name"]))), None)
    if item.get("name") and email:
        channel_type = BusinessChannelType.NAMED_PERSON_EMAIL
    if channel_type in {BusinessChannelType.PARTNERSHIP_EMAIL, BusinessChannelType.BUSINESS_DEVELOPMENT_EMAIL, BusinessChannelType.GENERAL_BUSINESS_EMAIL, BusinessChannelType.CONTACT_FORM, BusinessChannelType.SALES_MARKETING_EMAIL, BusinessChannelType.SUPPORT_EMAIL, BusinessChannelType.RESTRICTED_EMAIL, BusinessChannelType.OTHER}:
        contact_type = ContactType.GENERIC_BUSINESS_CONTACT
    else:
        contact_type = ContactType.PERSON
    unsuitable = channel_type in {BusinessChannelType.SUPPORT_EMAIL, BusinessChannelType.RESTRICTED_EMAIL, BusinessChannelType.OTHER}
    confidence = "HIGH" if source and evidence and _source_belongs_to_company(source, company.normalized_domain) else "MEDIUM"
    score, reason = rank_contact(company, normalize_role(named_match.title if named_match else None), contact_type, bool(email))
    contact = Contact(
        company_id=company.id, name=named_match.name if named_match else (str(item.get("name") or "") or None),
        normalized_name=named_match.normalized_name if named_match else normalize_name(str(item.get("name") or "")),
        title=named_match.title if named_match else str(item.get("title") or "") or None,
        normalized_role=named_match.normalized_role if named_match else (normalize_role(str(item.get("title") or "")) if item.get("name") else _generic_mailbox_role(email)),
        email=email, normalized_email=email, contact_type=contact_type,
        source_type="FIRST_PARTY_WEBSITE", source_url=source,
        extraction_method="CHANNEL_DISCOVERY", evidence_text=evidence,
        channel_type=channel_type, channel_url=target if is_form else None,
        purpose=str(item.get("purpose") or "")[:1000],
        validation_status=("OFFICIAL_EXTERNAL_FORM" if is_form and target and not _source_belongs_to_company(target, company.normalized_domain) else "NO_EMAIL") if not email else (
            "PUBLIC_EXTERNAL_DOMAIN" if email.split("@", 1)[1] != company.normalized_domain and not email.split("@",1)[1].endswith("." + company.normalized_domain) else "SYNTAX_VALID"
        ), confidence=confidence, ranking_score=score, ranking_reason=reason,
    )
    contact.company = company
    session.add(contact)
    session.flush()
    _add_evidence(session, contact, {"source_url": source, "extraction_method": "CHANNEL_DISCOVERY", "evidence": evidence, "confidence": confidence})
    return contact.id, True


def _recommend_primary(session: Session, company: Company) -> None:
    channels = [contact for contact in company.contacts if contact.channel_type is not None]
    for contact in channels:
        contact.recommended = False
    suitable = [contact for contact in channels if contact.channel_type not in {BusinessChannelType.SUPPORT_EMAIL, BusinessChannelType.RESTRICTED_EMAIL, BusinessChannelType.OTHER}]
    suitable.sort(key=lambda row: (
        _channel_rank(row),
        5 if row.validation_status == "PUBLIC_EXTERNAL_DOMAIN" else 0,
        1 if row.confidence == "HIGH" else 0,
        row.ranking_score,
    ), reverse=True)
    company.primary_channel = suitable[0] if suitable else None
    if company.primary_channel:
        company.primary_channel.recommended = True
        cross_domain_form = bool(company.primary_channel.channel_url and not _source_belongs_to_company(company.primary_channel.channel_url, company.normalized_domain))
        company.contactability_status = ContactabilityStatus.NEEDS_REVIEW if company.primary_channel.validation_status == "PUBLIC_EXTERNAL_DOMAIN" or cross_domain_form else ContactabilityStatus.READY_FOR_REVIEW
    else:
        company.contactability_status = ContactabilityStatus.NO_SUITABLE_CHANNEL


def _channel_rank(contact: Contact) -> int:
    if contact.channel_type != BusinessChannelType.CONTACT_FORM:
        return CHANNEL_RANK.get(contact.channel_type, 0)
    purpose = (contact.purpose or "").casefold()
    if re.search(r"partnership|collaboration", purpose):
        return 105
    if re.search(r"business inquiries|business enquiries|business development", purpose):
        return 95
    if re.search(r"sales|marketing", purpose):
        return 50
    return CHANNEL_RANK[BusinessChannelType.CONTACT_FORM]


def enrich_contacts(
    session: Session, *, priorities: Iterable[str] = ("HIGH", "MEDIUM"),
    reviews: Iterable[ReviewStatus] | None = None, limit: int = 25,
    fallback_settings: Crawl4AIFallbackSettings | None = None,
    fallback_adapter: Crawl4AIAdapter | None = None,
    user_agent: str = "GEOResearchOutreachAgent/0.2 (academic research; no outreach)",
) -> EnrichmentSummary:
    summary = EnrichmentSummary()
    query = select(Company).where(Company.priority_tier.in_([p.upper() for p in priorities])).order_by(Company.priority_score.desc(), Company.id).limit(limit)
    companies = list(session.scalars(query).unique())
    if reviews:
        allowed = set(reviews)
        companies = [company for company in companies if company.review_status in allowed]
    settings = fallback_settings or Crawl4AIFallbackSettings()
    fallback_adapter = fallback_adapter or Crawl4AIAdapter(settings, user_agent)
    fallback_count = 0
    for company in companies:
        summary.processed += 1
        snapshot = session.scalar(select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id).order_by(WebsiteSnapshot.fetched_at.desc()))
        if snapshot and snapshot.fetch_status in {"BLOCKED", "ROBOTS_DENIED"}:
            company.contact_status = ContactDiscoveryStatus.BLOCKED
            company.contactability_status = ContactabilityStatus.FETCH_FAILED
            summary.blocked += 1
            continue
        if snapshot and snapshot.fetch_status == "FETCH_FAILED":
            company.contact_status = ContactDiscoveryStatus.FAILED
            company.contactability_status = ContactabilityStatus.FETCH_FAILED
            company.contacts_enriched_at = datetime.now(timezone.utc)
            summary.failed += 1
            continue
        if not _site_matches_company(company, snapshot):
            company.contact_status = ContactDiscoveryStatus.CONTACT_NOT_FOUND
            company.contactability_status = ContactabilityStatus.NEEDS_REVIEW
            company.contacts_enriched_at = datetime.now(timezone.utc)
            summary.not_found += 1
            continue
        evidence: list[dict[str, str | None]] = []
        for page in snapshot.pages if snapshot else []:
            try:
                evidence.extend(json.loads(page.contact_evidence or "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
        # Backward-compatible cache extraction: addresses in stored visible text are explicit.
        if not evidence:
            for page in snapshot.pages if snapshot else []:
                for email in EMAIL_RE.findall(page.visible_text or ""):
                    evidence.append({"name": None, "title": None, "email": email, "contact_type": "GENERIC_BUSINESS_CONTACT", "source_url": page.final_url or page.requested_url, "evidence": email})
        created_for_company = 0
        for item in evidence:
            created, duplicate = _persist_candidate(session, company, item)
            created_for_company += int(created)
            summary.contacts_created += int(created)
            summary.duplicates += int(duplicate)
        channel_rows: list[dict[str, object]] = []
        for page in snapshot.pages if snapshot else []:
            try:
                channel_rows.extend(json.loads(page.extracted_channels or "[]"))
            except (json.JSONDecodeError, TypeError):
                continue
            if not page.extracted_channels or page.extracted_channels == "[]":
                # Legacy snapshots lack serialized channel evidence. Re-parse their
                # contact evidence (which retains markup context), not visible text;
                # visible text can put distant questions beside an email address.
                try:
                    legacy_evidence = json.loads(page.contact_evidence or "[]")
                except (json.JSONDecodeError, TypeError):
                    legacy_evidence = []
                for item in legacy_evidence:
                    email = normalize_email(str(item.get("email") or ""))
                    if email:
                        channel_rows.append({
                            "email": email, "name": item.get("name"), "title": item.get("title"),
                            "type": BusinessChannelType.NAMED_PERSON_EMAIL if item.get("contact_type") == "PERSON" else classify_channel(
                                str(item.get("evidence") or ""), email,
                            ),
                            "url": item.get("source_url") or page.final_url or page.requested_url,
                            "source_url": item.get("source_url") or page.final_url or page.requested_url,
                            "evidence": item.get("evidence") or email,
                            "purpose": item.get("evidence") or "",
                        })
        represented_emails = {normalize_email(str(row.get("email") or "")) for row in channel_rows}
        for contact in list(company.contacts):
            if not contact.email or contact.channel_type is not None or contact.normalized_email in represented_emails:
                continue
            evidence_text = contact.evidence_text or contact.email
            channel_type = BusinessChannelType.NAMED_PERSON_EMAIL if contact.contact_type == ContactType.PERSON else classify_channel(
                f"{evidence_text} {contact.source_url or ''}", contact.email,
            )
            channel_rows.append({
                "email": contact.email, "name": contact.name, "title": contact.title,
                "type": channel_type, "url": contact.source_url or company.website,
                "source_url": contact.source_url or company.website,
                "evidence": evidence_text, "purpose": evidence_text[:500],
            })
        for row in channel_rows:
            _, created = _store_channel(session, company, row)
            summary.contacts_created += int(created)
        # Upgrade an earlier catch-all classification only when the saved
        # first-party evidence explicitly names the person matching the mailbox.
        for contact in list(company.contacts):
            if contact.channel_type != BusinessChannelType.OTHER or not contact.email:
                continue
            matched_name = _nearby_person_text(contact.evidence_text or "", contact.email)
            if matched_name:
                contact.channel_type = BusinessChannelType.NAMED_PERSON_EMAIL
                contact.name = matched_name
                contact.normalized_name = normalize_name(matched_name)
                contact.contact_type = ContactType.PERSON
                contact.normalized_role = normalize_role(contact.title)
        urls = _fallback_urls(snapshot) if snapshot else []
        prior_success = session.scalar(select(ContactExtractionRun.id).where(
            ContactExtractionRun.company_id == company.id,
            ContactExtractionRun.method == "CRAWL4AI_MARKDOWN",
            ContactExtractionRun.status == "SUCCESS",
            ContactExtractionRun.created_at >= snapshot.fetched_at,
        ).limit(1)) if snapshot else None
        if settings.enabled and snapshot and not prior_success and not _has_named_person(company, evidence) and urls and fallback_count < settings.max_companies_per_run:
            fallback_count += 1
            summary.fallback_triggered += 1
            result = fallback_adapter.extract(urls[:settings.max_pages_per_company])
            fallback_created = 0
            for item in result.candidates:
                created, duplicate = _persist_candidate(session, company, item)
                fallback_created += int(created)
                created_for_company += int(created)
                summary.contacts_created += int(created)
                summary.duplicates += int(duplicate)
            session.add(ContactExtractionRun(
                company_id=company.id, method="CRAWL4AI_MARKDOWN", trigger_reason="no defensible named person in lightweight evidence",
                status=result.status, pages_processed=result.pages_processed, runtime_seconds=result.runtime_seconds,
                contacts_added=fallback_created, evidence_rejected=result.rejected, error_message=result.error,
            ))
            summary.fallback_pages += result.pages_processed
            summary.fallback_contacts_created += fallback_created
            summary.fallback_evidence_rejected += result.rejected
            summary.fallback_runtime_seconds += result.runtime_seconds
            if result.status == "SUCCESS":
                summary.fallback_succeeded += 1
            else:
                summary.fallback_failed += 1
        company.contacts_enriched_at = datetime.now(timezone.utc)
        _recommend_primary(session, company)
        if created_for_company or company.contacts:
            company.contact_status = ContactDiscoveryStatus.CONTACT_FOUND
            summary.found += 1
        else:
            company.contact_status = ContactDiscoveryStatus.CONTACT_NOT_FOUND
            summary.not_found += 1
    session.commit()
    return summary
