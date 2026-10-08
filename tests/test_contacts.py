import json

from outreach_agent.contacts import (
    enrich_contacts, extract_contact_evidence, normalize_email, normalize_role, rank_contact,
)
from outreach_agent.models import (
    Company, ContactDiscoveryStatus, ContactType, DiscoveryRecord, ReviewStatus, WebsitePage, WebsiteSnapshot,
)


def company(**overrides):
    values = dict(
        company_name="Example Co", normalized_name="example co", website="https://example.com",
        normalized_domain="example.com", priority_tier="HIGH", priority_score=90,
    )
    values.update(overrides)
    return Company(**values)


def test_normalization_and_role_taxonomy():
    assert normalize_email(" Jane.Doe@Example.COM. ") == "jane.doe@example.com"
    assert normalize_email("not-an-address") is None
    assert normalize_role("Co-Founder & CEO") == "FOUNDER_OWNER"
    assert normalize_role("VP, Content Strategy") == "SEO_CONTENT"
    assert normalize_role(None) == "UNKNOWN"


def test_extracts_explicit_person_and_generic_contacts_without_guessing():
    markup = """
    <section class="team-member"><h3>Jane Doe</h3><strong>Founder</strong>
      <a href="mailto:jane@example.com">Email Jane</a></section>
    <footer><a href="mailto:hello@example.com">Contact us</a></footer>
    <section><h3>Sam Smith</h3><strong>Marketing Director</strong></section>
    """
    rows = extract_contact_evidence(markup, "https://example.com/team")
    assert any(row["name"] == "Jane Doe" and row["contact_type"] == "PERSON" for row in rows)
    assert any(row["email"] == "hello@example.com" and row["contact_type"] == "GENERIC_BUSINESS_CONTACT" for row in rows)
    assert not any(row["name"] == "Sam Smith" for row in rows)  # no invented email/contact record


def test_context_changes_role_ranking(session):
    startup = company()
    local = company(normalized_domain="local.test")
    local.discoveries.append(DiscoveryRecord(source_type="growthzone_public", source_identifier="x"))
    startup_marketing, _ = rank_contact(startup, "MARKETING_GROWTH", ContactType.PERSON, True)
    startup_founder, _ = rank_contact(startup, "FOUNDER_OWNER", ContactType.PERSON, True)
    local_manager, _ = rank_contact(local, "EXECUTIVE", ContactType.PERSON, True)
    local_marketing, _ = rank_contact(local, "MARKETING_GROWTH", ContactType.PERSON, True)
    assert startup_founder > startup_marketing
    assert local_manager > local_marketing


def test_cached_enrichment_is_idempotent_and_not_found_is_valid(session):
    found = company()
    missing = company(company_name="Missing", normalized_name="missing", normalized_domain="missing.test", priority_score=80)
    low = company(company_name="Low", normalized_name="low", normalized_domain="low.test", priority_tier="LOW", priority_score=10)
    session.add_all([found, missing, low])
    session.flush()
    snapshot = WebsiteSnapshot(company_id=found.id, requested_url=found.website, fetch_status="SUCCESS")
    snapshot.pages.append(WebsitePage(
        requested_url=found.website, final_url=found.website, http_status=200, content_type="text/html",
        visible_text="Jane Doe Founder jane@example.com", visible_text_length=33, fetch_status="SUCCESS",
        contact_evidence=json.dumps([{
            "name": "Jane Doe", "title": "Founder", "email": "jane@example.com", "contact_type": "PERSON",
            "source_url": "https://example.com/team", "evidence": "Jane Doe Founder jane@example.com",
        }]),
    ))
    session.add(snapshot)
    session.commit()

    first = enrich_contacts(session, limit=10)
    second = enrich_contacts(session, limit=10)
    assert first.contacts_created == 1
    assert second.contacts_created == 0
    assert second.duplicates == 1
    assert found.contact_status == ContactDiscoveryStatus.CONTACT_FOUND
    assert missing.contact_status == ContactDiscoveryStatus.CONTACT_NOT_FOUND
    assert low.contact_status == ContactDiscoveryStatus.NOT_RUN
    assert found.contacts[0].review_status == ReviewStatus.PENDING
    assert found.contacts[0].source_url == "https://example.com/team"
