from outreach_agent.config import Crawl4AIFallbackSettings
from outreach_agent.contacts import enrich_contacts
from outreach_agent.crawl4ai_adapter import Crawl4AIAdapter, FallbackResult, extract_markdown_people
from outreach_agent.models import (
    Company, Contact, ContactEvidence, ContactExtractionRun, ContactType, ReviewStatus,
    WebsitePage, WebsiteSnapshot,
)


class FakeAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def extract(self, urls):
        self.calls.append(urls)
        return self.result


def _company():
    return Company(
        company_name="Example Co", normalized_name="example co", website="https://example.com",
        normalized_domain="example.com", priority_tier="HIGH", priority_score=90,
    )


def _snapshot(company, text="Example Co — meet our founder"):
    snapshot = WebsiteSnapshot(company_id=company.id, requested_url=company.website, fetch_status="SUCCESS")
    snapshot.pages.append(WebsitePage(
        requested_url="https://example.com/about", final_url="https://example.com/about",
        http_status=200, content_type="text/html", visible_text=text,
        visible_text_length=len(text), fetch_status="SUCCESS", contact_evidence="[]",
    ))
    return snapshot


def test_markdown_extractor_is_narrow_and_evidenced():
    found, rejected = extract_markdown_people("## Jane Doe\nFounder & CEO\n\n## Our Team\nMarketing Director", "https://example.com/about")
    assert [(row["name"], row["title"]) for row in found] == [("Jane Doe", "Founder")]
    assert found[0]["extraction_method"] == "CRAWL4AI_MARKDOWN"
    assert rejected == 0


def test_optional_package_unavailable_is_a_recordable_result(monkeypatch):
    adapter = Crawl4AIAdapter(Crawl4AIFallbackSettings(enabled=True), "test-agent")
    monkeypatch.setattr(adapter, "available", lambda: False)
    result = adapter.extract(["https://example.com/about"])
    assert result.status == "UNAVAILABLE"
    assert result.pages_processed == 0 and result.error


def test_fallback_triggers_and_persists_provenance(session):
    target = _company()
    session.add(target)
    session.flush()
    session.add(_snapshot(target))
    session.commit()
    candidate = {
        "name": "Jane Doe", "title": "Founder", "email": None, "contact_type": "PERSON",
        "source_url": "https://example.com/about", "evidence": "Jane Doe | Founder",
        "extraction_method": "CRAWL4AI_MARKDOWN", "confidence": "HIGH",
    }
    adapter = FakeAdapter(FallbackResult(status="SUCCESS", pages_processed=1, runtime_seconds=.25, candidates=[candidate]))
    summary = enrich_contacts(
        session, limit=1, fallback_settings=Crawl4AIFallbackSettings(enabled=True), fallback_adapter=adapter,
    )
    assert summary.fallback_triggered == summary.fallback_succeeded == 1
    assert summary.fallback_contacts_created == 1
    assert target.contacts[0].name == "Jane Doe"
    assert target.contacts[0].extraction_method == "CRAWL4AI_MARKDOWN"
    assert session.query(ContactEvidence).count() == 1
    run = session.query(ContactExtractionRun).one()
    assert run.status == "SUCCESS" and run.contacts_added == 1


def test_named_lightweight_contact_prevents_fallback(session):
    target = _company()
    session.add(target)
    session.flush()
    snapshot = _snapshot(target)
    snapshot.pages[0].contact_evidence = '[{"name":"Jane Doe","title":"Founder","email":null,"contact_type":"PERSON","source_url":"https://example.com/about","evidence":"Jane Doe Founder"}]'
    session.add(snapshot)
    session.commit()
    adapter = FakeAdapter(FallbackResult(status="SUCCESS"))
    summary = enrich_contacts(session, limit=1, fallback_settings=Crawl4AIFallbackSettings(enabled=True), fallback_adapter=adapter)
    assert summary.fallback_triggered == 0
    assert adapter.calls == []


def test_fallback_failure_is_recorded_without_overwriting_http_outcome(session):
    target = _company()
    session.add(target)
    session.flush()
    session.add(_snapshot(target))
    session.commit()
    adapter = FakeAdapter(FallbackResult(status="TIMEOUT", error="bounded timeout"))
    summary = enrich_contacts(session, limit=1, fallback_settings=Crawl4AIFallbackSettings(enabled=True), fallback_adapter=adapter)
    assert summary.failed == 0 and summary.fallback_failed == 1
    assert session.query(ContactExtractionRun).one().status == "TIMEOUT"


def test_successful_fallback_is_resumable_for_unchanged_snapshot(session):
    target = _company()
    session.add(target)
    session.flush()
    session.add(_snapshot(target))
    session.commit()
    adapter = FakeAdapter(FallbackResult(status="SUCCESS", pages_processed=1))
    settings = Crawl4AIFallbackSettings(enabled=True)
    assert enrich_contacts(session, limit=1, fallback_settings=settings, fallback_adapter=adapter).fallback_triggered == 1
    assert enrich_contacts(session, limit=1, fallback_settings=settings, fallback_adapter=adapter).fallback_triggered == 0
    assert len(adapter.calls) == 1


def test_duplicate_fallback_evidence_preserves_human_review(session):
    target = _company()
    session.add(target)
    session.flush()
    existing = Contact(
        company_id=target.id, name="Jane Doe", normalized_name="jane doe", title="Owner",
        normalized_role="FOUNDER_OWNER", contact_type=ContactType.PERSON, review_status=ReviewStatus.APPROVED,
        review_notes="verified", source_url="https://example.com/team", extraction_method="LIGHTWEIGHT_HTML",
    )
    session.add(existing)
    session.flush()
    session.add(_snapshot(target))
    session.commit()
    candidate = {
        "name": "Jane Doe", "title": "Founder", "email": None, "contact_type": "PERSON",
        "source_url": "https://example.com/about", "evidence": "Jane Doe | Founder",
        "extraction_method": "CRAWL4AI_MARKDOWN", "confidence": "HIGH",
    }
    adapter = FakeAdapter(FallbackResult(status="SUCCESS", pages_processed=1, candidates=[candidate]))
    # Existing named contact suppresses fallback, so exercise shared persistence via cached evidence.
    snapshot = session.query(WebsiteSnapshot).filter_by(company_id=target.id).one()
    snapshot.pages[0].contact_evidence = __import__("json").dumps([candidate])
    enrich_contacts(session, limit=1)
    session.refresh(existing)
    assert existing.review_status == ReviewStatus.APPROVED
    assert existing.review_notes == "verified" and existing.title == "Owner"
    assert session.query(ContactEvidence).filter_by(contact_id=existing.id).count() == 1
