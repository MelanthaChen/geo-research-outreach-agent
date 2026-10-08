from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from outreach_agent.models import (
    Company, Contact, ContactDiscoveryStatus, ContactType, ReviewStatus, WebsiteSnapshot,
)

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
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


@dataclass
class EnrichmentSummary:
    processed: int = 0
    found: int = 0
    not_found: int = 0
    blocked: int = 0
    failed: int = 0
    contacts_created: int = 0
    duplicates: int = 0


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
        if any(term in folded for term in terms):
            return role
    return "OTHER" if folded else "UNKNOWN"


def _plausible_name(value: str) -> bool:
    words = value.strip().split()
    folded = value.casefold().strip(" .:-")
    banned_terms = {
        "about", "apply", "committee", "contact", "content", "copyright", "executive", "faq",
        "frequently", "functions", "leadership", "management", "our", "questions", "statement",
        "team", "terms", "your",
    }
    return (
        2 <= len(words) <= 4 and len(value) <= 60
        and not any(term in banned_terms for term in folded.split())
        and all(any(ch.isalpha() for ch in word) and (word[0].isupper() or word.isupper()) for word in words)
    )


def _nearby_person(container, email: str) -> tuple[str | None, str | None]:
    """Require a personal local-part to agree with a nearby explicit full name."""
    local = email.split("@", 1)[0].split("+", 1)[0].casefold()
    if local in GENERIC_LOCAL_PARTS:
        return None, None
    text = " ".join(container.get_text(" ", strip=True).split())[:1200]
    names = re.findall(r"\b([A-Z][A-Za-z'’-]{1,30}\s+[A-Z][A-Za-z'’-]{1,30}(?:\s+[A-Z][A-Za-z'’-]{1,30})?)\b", text)
    def local_matches(candidate: str) -> bool:
        first = normalize_name(candidate).split()[0]
        return local == first or any(local.startswith(first + separator) for separator in (".", "_", "-"))
    name = next((candidate for candidate in names if _plausible_name(candidate) and local_matches(candidate)), None)
    if not name:
        return None, None
    short_nodes = [" ".join(node.get_text(" ", strip=True).split()) for node in container.select("h1,h2,h3,h4,h5,h6,strong,b")]
    title = next((item for item in short_nodes if len(item) <= 100 and normalize_role(item) not in {"UNKNOWN", "OTHER"}), None)
    return name, title


def _standalone_people(soup: BeautifulSoup, source_url: str) -> list[ContactCandidate]:
    results: list[ContactCandidate] = []
    for node in soup.select("h1,h2,h3,h4,h5,h6,strong,b"):
        name = " ".join(node.get_text(" ", strip=True).split())
        if not _plausible_name(name):
            continue
        neighbors = [node.find_previous_sibling(), node.find_next_sibling()]
        texts = [" ".join(item.get_text(" ", strip=True).split()) for item in neighbors if item]
        title = next((item for item in texts if len(item) <= 100 and normalize_role(item) not in {"UNKNOWN", "OTHER"}), None)
        if title:
            results.append(ContactCandidate(name, title, None, "PERSON", source_url, f"{name} — {title}"))
    return results


def extract_contact_evidence(markup: str, source_url: str) -> list[dict[str, str | None]]:
    """Extract only explicit public contact facts; never derive names or emails."""
    soup = BeautifulSoup(markup, "html.parser")
    candidates: list[ContactCandidate] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
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


def enrich_contacts(
    session: Session, *, priorities: Iterable[str] = ("HIGH", "MEDIUM"),
    reviews: Iterable[ReviewStatus] | None = None, limit: int = 25,
) -> EnrichmentSummary:
    summary = EnrichmentSummary()
    query = select(Company).where(Company.priority_tier.in_([p.upper() for p in priorities])).order_by(Company.priority_score.desc(), Company.id).limit(limit)
    companies = list(session.scalars(query).unique())
    if reviews:
        allowed = set(reviews)
        companies = [company for company in companies if company.review_status in allowed]
    for company in companies:
        summary.processed += 1
        snapshot = session.scalar(select(WebsiteSnapshot).where(WebsiteSnapshot.company_id == company.id).order_by(WebsiteSnapshot.fetched_at.desc()))
        if snapshot and snapshot.fetch_status in {"BLOCKED", "ROBOTS_DENIED"}:
            company.contact_status = ContactDiscoveryStatus.BLOCKED
            summary.blocked += 1
            continue
        if snapshot and snapshot.fetch_status == "FETCH_FAILED":
            company.contact_status = ContactDiscoveryStatus.FAILED
            company.contacts_enriched_at = datetime.now(timezone.utc)
            summary.failed += 1
            continue
        if not _site_matches_company(company, snapshot):
            company.contact_status = ContactDiscoveryStatus.CONTACT_NOT_FOUND
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
            email = normalize_email(item.get("email"))
            name = item.get("name") or None
            kind = ContactType(item.get("contact_type") or "GENERIC_BUSINESS_CONTACT")
            if name and not _plausible_name(name):
                name = None
                kind = ContactType.GENERIC_BUSINESS_CONTACT
            if name and email:
                local = email.split("@", 1)[0].casefold()
                first = normalize_name(name).split()[0]
                matches = local == first or any(local.startswith(first + separator) for separator in (".", "_", "-"))
                if local in GENERIC_LOCAL_PARTS or not matches:
                    name = None
                    kind = ContactType.GENERIC_BUSINESS_CONTACT
            title = item.get("title") if name else None
            role = normalize_role(title)
            if kind == ContactType.GENERIC_BUSINESS_CONTACT and role in {"UNKNOWN", "OTHER"}:
                role = _generic_mailbox_role(email)
            if not name and not email:
                continue
            existing = session.scalar(select(Contact).where(Contact.normalized_email == email)) if email else session.scalar(
                select(Contact).where(Contact.company_id == company.id, Contact.normalized_name == normalize_name(name), Contact.normalized_role == role)
            )
            if existing:
                if name and existing.contact_type == ContactType.GENERIC_BUSINESS_CONTACT:
                    existing.name = name
                    existing.normalized_name = normalize_name(name)
                    existing.title = title
                    existing.normalized_role = role
                    existing.contact_type = ContactType.PERSON
                    existing.confidence = "HIGH"
                    existing.source_url = item.get("source_url")
                    existing.ranking_score, existing.ranking_reason = rank_contact(company, role, ContactType.PERSON, bool(email))
                summary.duplicates += 1
                continue
            score, reason = rank_contact(company, role, kind, bool(email))
            local = (email or "").split("@", 1)[0]
            unsuitable = local in {"accessibility", "billing", "careers", "dpo", "legal", "privacy", "support", "webmaster"}
            if unsuitable:
                score = max(0, score - 20)
                reason += "; low-suitability mailbox"
            email_domain = (email or "@").split("@", 1)[-1]
            validation = "NO_EMAIL" if not email else ("SYNTAX_VALID" if email_domain == company.normalized_domain or email_domain.endswith("." + company.normalized_domain) else "PUBLIC_EXTERNAL_DOMAIN")
            session.add(Contact(
                company_id=company.id, name=name, normalized_name=normalize_name(name), title=title,
                normalized_role=role, email=email, normalized_email=email, contact_type=kind,
                source_type="FIRST_PARTY_WEBSITE", source_url=item.get("source_url"),
                validation_status=validation, confidence="HIGH" if name and (email or role != "UNKNOWN") else ("LOW" if unsuitable else "MEDIUM"),
                ranking_score=score, ranking_reason=reason,
            ))
            created_for_company += 1
            summary.contacts_created += 1
        company.contacts_enriched_at = datetime.now(timezone.utc)
        if created_for_company or company.contacts:
            company.contact_status = ContactDiscoveryStatus.CONTACT_FOUND
            summary.found += 1
        else:
            company.contact_status = ContactDiscoveryStatus.CONTACT_NOT_FOUND
            summary.not_found += 1
    session.commit()
    return summary
