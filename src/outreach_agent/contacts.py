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
    "admin", "business", "contact", "hello", "info", "marketing", "office", "partners",
    "partnerships", "sales", "support", "team",
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
    banned = {"about", "contact", "leadership", "management", "our team", "team"}
    return 2 <= len(words) <= 5 and value.casefold().strip() not in banned and all(any(ch.isalpha() for ch in word) for word in words)


def extract_contact_evidence(markup: str, source_url: str) -> list[dict[str, str | None]]:
    """Extract only explicit public contact facts; never derive names or emails."""
    soup = BeautifulSoup(markup, "html.parser")
    candidates: list[ContactCandidate] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for anchor in soup.select('a[href^="mailto:" i]'):
        email = normalize_email(str(anchor.get("href", "")).split(":", 1)[-1].split("?", 1)[0])
        if not email:
            continue
        container = anchor.find_parent(["article", "li", "section", "div"]) or anchor.parent
        text = " ".join(container.get_text(" ", strip=True).split())[:600] if container else ""
        name = title = None
        if email.split("@", 1)[0] not in GENERIC_LOCAL_PARTS and container:
            headings = [" ".join(node.get_text(" ", strip=True).split()) for node in container.select("h1,h2,h3,h4,strong,b")]
            name = next((item for item in headings if _plausible_name(item)), None)
            title = next((item for item in headings + [text] if normalize_role(item) not in {"UNKNOWN", "OTHER"}), None)
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
    return [asdict(candidate) for candidate in candidates]


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
            role = normalize_role(item.get("title"))
            kind = ContactType(item.get("contact_type") or "GENERIC_BUSINESS_CONTACT")
            existing = session.scalar(select(Contact).where(Contact.normalized_email == email)) if email else session.scalar(
                select(Contact).where(Contact.company_id == company.id, Contact.normalized_name == normalize_name(name), Contact.normalized_role == role)
            )
            if existing:
                summary.duplicates += 1
                continue
            score, reason = rank_contact(company, role, kind, bool(email))
            session.add(Contact(
                company_id=company.id, name=name, normalized_name=normalize_name(name), title=item.get("title"),
                normalized_role=role, email=email, normalized_email=email, contact_type=kind,
                source_type="FIRST_PARTY_WEBSITE", source_url=item.get("source_url"),
                validation_status="SYNTAX_VALID" if email else "NO_EMAIL", confidence="HIGH" if name and email else "MEDIUM",
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
