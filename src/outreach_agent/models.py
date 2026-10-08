from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CompanyStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY_FOR_GEO = "READY_FOR_GEO"


class OutreachStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MAYBE = "MAYBE"


class ContactType(str, enum.Enum):
    PERSON = "PERSON"
    GENERIC_BUSINESS_CONTACT = "GENERIC_BUSINESS_CONTACT"


class BusinessChannelType(str, enum.Enum):
    PARTNERSHIP_EMAIL = "PARTNERSHIP_EMAIL"
    BUSINESS_DEVELOPMENT_EMAIL = "BUSINESS_DEVELOPMENT_EMAIL"
    GENERAL_BUSINESS_EMAIL = "GENERAL_BUSINESS_EMAIL"
    NAMED_PERSON_EMAIL = "NAMED_PERSON_EMAIL"
    CONTACT_FORM = "CONTACT_FORM"
    SALES_MARKETING_EMAIL = "SALES_MARKETING_EMAIL"
    SUPPORT_EMAIL = "SUPPORT_EMAIL"
    RESTRICTED_EMAIL = "RESTRICTED_EMAIL"
    OTHER = "OTHER"


class ContactabilityStatus(str, enum.Enum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NO_SUITABLE_CHANNEL = "NO_SUITABLE_CHANNEL"
    FETCH_FAILED = "FETCH_FAILED"


class ContactDiscoveryStatus(str, enum.Enum):
    NOT_RUN = "NOT_RUN"
    CONTACT_FOUND = "CONTACT_FOUND"
    CONTACT_NOT_FOUND = "CONTACT_NOT_FOUND"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


ALLOWED_COMPANY_TRANSITIONS: dict[CompanyStatus, set[CompanyStatus]] = {
    CompanyStatus.DISCOVERED: {CompanyStatus.QUALIFIED, CompanyStatus.NEEDS_REVIEW, CompanyStatus.REJECTED},
    CompanyStatus.NEEDS_REVIEW: {CompanyStatus.QUALIFIED, CompanyStatus.REJECTED},
    CompanyStatus.QUALIFIED: {CompanyStatus.NEEDS_REVIEW, CompanyStatus.REJECTED, CompanyStatus.READY_FOR_GEO},
    CompanyStatus.REJECTED: {CompanyStatus.NEEDS_REVIEW, CompanyStatus.QUALIFIED},
    CompanyStatus.READY_FOR_GEO: set(),
}


def transition_company(company: "Company", target: CompanyStatus) -> None:
    """Apply a deliberate company-lifecycle transition or reject it."""
    current = company.pipeline_status
    if target == current:
        return
    if target not in ALLOWED_COMPANY_TRANSITIONS[current]:
        raise ValueError(f"Invalid company status transition: {current.value} -> {target.value}")
    company.pipeline_status = target


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    website: Mapped[str] = mapped_column(String(2048))
    normalized_domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(255))
    industry_raw: Mapped[str | None] = mapped_column(String(255))
    industry_normalized: Mapped[str | None] = mapped_column(String(100))
    location: Mapped[str | None] = mapped_column(String(255))
    company_size: Mapped[str | None] = mapped_column(String(100))
    company_size_category: Mapped[str] = mapped_column(String(50), default="UNKNOWN")
    company_size_value: Mapped[int | None] = mapped_column(Integer)
    company_size_source: Mapped[str] = mapped_column(String(50), default="unknown")
    qualification_score: Mapped[float | None] = mapped_column(Float)
    qualification_reason: Mapped[str | None] = mapped_column(Text)
    geo_opportunity: Mapped[str | None] = mapped_column(String(100))
    eligibility: Mapped[str | None] = mapped_column(String(50))
    geo_opportunity_reason: Mapped[str | None] = mapped_column(Text)
    priority_score: Mapped[float | None] = mapped_column(Float)
    priority_tier: Mapped[str | None] = mapped_column(String(50), index=True)
    evidence_confidence: Mapped[str | None] = mapped_column(String(50))
    qualification_evidence: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING, index=True)
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    contact_status: Mapped[ContactDiscoveryStatus] = mapped_column(
        Enum(ContactDiscoveryStatus), default=ContactDiscoveryStatus.NOT_RUN, index=True
    )
    contacts_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contactability_status: Mapped[ContactabilityStatus] = mapped_column(
        Enum(ContactabilityStatus), default=ContactabilityStatus.NO_SUITABLE_CHANNEL, index=True
    )
    primary_channel_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", use_alter=True, name="fk_company_primary_channel"), nullable=True)
    channel_override_contact_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_override_reason: Mapped[str | None] = mapped_column(Text)
    pipeline_status: Mapped[CompanyStatus] = mapped_column(Enum(CompanyStatus), default=CompanyStatus.DISCOVERED)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    discoveries: Mapped[list[DiscoveryRecord]] = relationship(back_populates="company", cascade="all, delete-orphan")
    contacts: Mapped[list[Contact]] = relationship(back_populates="company", cascade="all, delete-orphan", foreign_keys="Contact.company_id")
    primary_channel: Mapped[Contact | None] = relationship(foreign_keys=[primary_channel_id], post_update=True)
    contact_extraction_runs: Mapped[list[ContactExtractionRun]] = relationship(cascade="all, delete-orphan")


class DiscoveryRecord(Base):
    __tablename__ = "discovery_records"
    __table_args__ = (UniqueConstraint("company_id", "source_type", "source_identifier", name="uq_discovery_provenance"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(50))
    source_name: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_identifier: Mapped[str] = mapped_column(String(2048))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_data: Mapped[str | None] = mapped_column(Text)
    company: Mapped[Company] = relationship(back_populates="discoveries")


class WebsiteSnapshot(Base):
    __tablename__ = "website_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    requested_url: Mapped[str] = mapped_column(String(2048))
    final_url: Mapped[str | None] = mapped_column(String(2048))
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    content_type: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(1000))
    meta_description: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    visible_text_length: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    robots_present: Mapped[bool] = mapped_column(Boolean, default=False)
    sitemap_present: Mapped[bool] = mapped_column(Boolean, default=False)
    structured_data_types: Mapped[str] = mapped_column(Text, default="[]")
    extracted_signals: Mapped[str] = mapped_column(Text, default="{}")
    fetch_status: Mapped[str] = mapped_column(String(50), index=True)
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    used_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    pages: Mapped[list[WebsitePage]] = relationship(cascade="all, delete-orphan", back_populates="snapshot")


class WebsitePage(Base):
    __tablename__ = "website_pages"
    __table_args__ = (UniqueConstraint("snapshot_id", "requested_url", name="uq_snapshot_page_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("website_snapshots.id"), index=True)
    requested_url: Mapped[str] = mapped_column(String(2048))
    final_url: Mapped[str | None] = mapped_column(String(2048))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(1000))
    meta_description: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    visible_text: Mapped[str | None] = mapped_column(Text)
    contact_evidence: Mapped[str] = mapped_column(Text, default="[]")
    extracted_channels: Mapped[str] = mapped_column(Text, default="[]")
    visible_text_length: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    structured_data_types: Mapped[str] = mapped_column(Text, default="[]")
    fetch_status: Mapped[str] = mapped_column(String(50))
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    snapshot: Mapped[WebsiteSnapshot] = relationship(back_populates="pages")


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    normalized_name: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    normalized_role: Mapped[str] = mapped_column(String(50), default="UNKNOWN", index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    normalized_email: Mapped[str | None] = mapped_column(String(320), unique=True)
    contact_type: Mapped[ContactType] = mapped_column(Enum(ContactType), default=ContactType.GENERIC_BUSINESS_CONTACT)
    source_type: Mapped[str | None] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    extraction_method: Mapped[str] = mapped_column(String(50), default="LIGHTWEIGHT_HTML")
    evidence_text: Mapped[str | None] = mapped_column(Text)
    channel_type: Mapped[BusinessChannelType | None] = mapped_column(Enum(BusinessChannelType), index=True)
    channel_url: Mapped[str | None] = mapped_column(String(2048))
    purpose: Mapped[str | None] = mapped_column(Text)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    validation_status: Mapped[str] = mapped_column(String(50), default="UNKNOWN")
    confidence: Mapped[str] = mapped_column(String(50), default="LOW")
    ranking_score: Mapped[float] = mapped_column(Float, default=0)
    ranking_reason: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING, index=True)
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    company: Mapped[Company] = relationship(back_populates="contacts", foreign_keys=[company_id])
    evidence_records: Mapped[list[ContactEvidence]] = relationship(back_populates="contact", cascade="all, delete-orphan")


class ContactEvidence(Base):
    __tablename__ = "contact_evidence"
    __table_args__ = (UniqueConstraint("contact_id", "source_url", "extraction_method", name="uq_contact_evidence_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    extraction_method: Mapped[str] = mapped_column(String(50))
    evidence_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(50), default="MEDIUM")
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    contact: Mapped[Contact] = relationship(back_populates="evidence_records")


class ContactExtractionRun(Base):
    __tablename__ = "contact_extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    method: Mapped[str] = mapped_column(String(50))
    trigger_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), index=True)
    pages_processed: Mapped[int] = mapped_column(Integer, default=0)
    runtime_seconds: Mapped[float] = mapped_column(Float, default=0)
    contacts_added: Mapped[int] = mapped_column(Integer, default=0)
    evidence_rejected: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outreach(Base):
    __tablename__ = "outreach"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    template_version: Mapped[str] = mapped_column(String(100))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[OutreachStatus] = mapped_column(Enum(OutreachStatus), default=OutreachStatus.DRAFT)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Response(Base):
    __tablename__ = "responses"

    id: Mapped[int] = mapped_column(primary_key=True)
    outreach_id: Mapped[int] = mapped_column(ForeignKey("outreach.id"), index=True)
    response_type: Mapped[str] = mapped_column(String(100))
    interest_status: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)
