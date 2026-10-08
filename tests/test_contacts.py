import json

from outreach_agent.contacts import (
    _channel_evidence, classify_channel, enrich_contacts, extract_contact_evidence,
    normalize_email, normalize_role, rank_contact,
)
from outreach_agent.models import (
    BusinessChannelType, Company, ContactDiscoveryStatus, ContactType, ContactabilityStatus,
    DiscoveryRecord, ReviewStatus, WebsitePage, WebsiteSnapshot,
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


def test_channel_evidence_handles_jsonld_type_arrays():
    markup = '''<script type="application/ld+json">{"@type":["Organization","LocalBusiness"],"email":"info@example.com"}</script>'''
    rows = _channel_evidence(markup, "https://example.com/")
    assert any(row.get("email") == "info@example.com" for row in rows)


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
    assert any(row["name"] == "Sam Smith" and row["email"] is None for row in rows)


def test_rejects_page_headings_as_people_and_keeps_shared_mailbox_generic():
    markup = """
    <main><h1>Frequently Asked Questions</h1><p>Still need help?
      <a href="mailto:accessibility@example.com">Email us</a></p></main>
    <section><h2>Jane Smith</h2><p>Head of Marketing</p>
      <a href="mailto:info@example.com">Contact Jane</a></section>
    """
    rows = extract_contact_evidence(markup, "https://example.com/faq")
    assert any(row["email"] == "accessibility@example.com" and row["name"] is None for row in rows)
    assert any(row["email"] == "info@example.com" and row["name"] is None for row in rows)
    assert any(row["name"] == "Jane Smith" and row["email"] is None for row in rows)
    assert not any(row["name"] == "Frequently Asked Questions" for row in rows)


def test_extracts_explicit_staff_member_without_inventing_email():
    markup = "<section><h3>Jane Smith</h3><p>Executive Director</p></section>"
    rows = extract_contact_evidence(markup, "https://example.com/team")
    assert rows == [{
        "name": "Jane Smith", "title": "Executive Director", "email": None,
        "contact_type": "PERSON", "source_url": "https://example.com/team",
        "evidence": "Jane Smith — Executive Director",
        "extraction_method": "LIGHTWEIGHT_HTML", "confidence": "HIGH",
    }]


def test_extracts_founder_biography_and_team_card_but_not_testimonial():
    markup = """
    <main><p>Sulan Zhang, co-founder, started the company in 2020.</p>
      <div class="team-card"><span class="name">Aly Moursy</span><span class="title">Founder & CEO</span></div>
      <div class="testimonial"><strong>Happy Customer</strong><p>Founder</p></div>
    </main>
    """
    rows = extract_contact_evidence(markup, "https://example.com/about")
    assert {row["name"] for row in rows if row["name"]} == {"Sulan Zhang", "Aly Moursy"}


def test_extracts_is_the_founder_sentence():
    rows = extract_contact_evidence(
        "<p>Jane Smith is the co-founder and CEO of Example Inc.</p>", "https://example.com/about",
    )
    assert any(row["name"] == "Jane Smith" and row["title"].casefold() == "co-founder" for row in rows)


def test_organization_listing_is_not_persisted_as_a_person(session):
    target = company()
    session.add(target)
    session.flush()
    snapshot = WebsiteSnapshot(company_id=target.id, requested_url=target.website, fetch_status="SUCCESS")
    snapshot.pages.append(WebsitePage(
        requested_url=target.website, final_url=target.website, http_status=200, content_type="text/html",
        visible_text="Example Co", visible_text_length=10, fetch_status="SUCCESS",
        contact_evidence=json.dumps([{
            "name": "Keller Williams", "title": "President", "email": None, "contact_type": "PERSON",
            "source_url": target.website, "evidence": "President Keller Williams CT Capital",
        }]),
    ))
    session.add(snapshot)
    session.commit()
    assert enrich_contacts(session, limit=1).contacts_created == 0


def test_channel_purpose_uses_page_context_not_mailbox_prefix():
    rows = _channel_evidence(
        '<section><h2>Partnerships</h2><p>For collaborations email <a href="mailto:hello@example.com">hello</a>.</p></section>',
        "https://example.com/contact",
    )
    email = next(row for row in rows if row.get("email"))
    assert email["type"] == BusinessChannelType.PARTNERSHIP_EMAIL
    assert classify_channel("Hello <hello@example.com>") == BusinessChannelType.OTHER


def test_generic_mailbox_classification_does_not_claim_partnership_intent():
    assert classify_channel("", "info@example.com") == BusinessChannelType.GENERAL_BUSINESS_EMAIL
    assert classify_channel("", "founders@example.com") == BusinessChannelType.GENERAL_BUSINESS_EMAIL
    assert classify_channel("", "partnerships@example.com") == BusinessChannelType.PARTNERSHIP_EMAIL
    assert classify_channel("", "privacy@example.com") == BusinessChannelType.RESTRICTED_EMAIL


def test_plain_visible_named_founder_email_is_extracted_as_person_channel():
    rows = _channel_evidence(
        "<section><h2>Sulan Zhang, co-founder</h2><p>Questions go to sulanzhangart@gmail.com</p></section>",
        "https://example.com/about",
    )
    channel = next(row for row in rows if row.get("email"))
    assert channel["email"] == "sulanzhangart@gmail.com"
    assert channel["name"] == "Sulan Zhang"
    assert channel["type"] == BusinessChannelType.NAMED_PERSON_EMAIL


def test_first_initial_surname_email_matches_explicit_named_contact():
    from outreach_agent.contacts import _nearby_person_text

    assert _nearby_person_text(
        "Contact Erica Perez with questions about the collection: eperez11@example.edu",
        "eperez11@example.edu",
    ) == "Erica Perez"


def test_legacy_channel_migration_uses_contact_evidence_not_whole_page_text(session):
    target = company()
    session.add(target)
    session.flush()
    snapshot = WebsiteSnapshot(company_id=target.id, requested_url=target.website, fetch_status="SUCCESS")
    snapshot.pages.append(WebsitePage(
        requested_url=target.website, final_url=target.website, http_status=200, content_type="text/html",
        visible_text="FAQ privacy support Email info@example.com", visible_text_length=45,
        contact_evidence=json.dumps([{
            "name": None, "title": None, "email": "info@example.com",
            "contact_type": "GENERIC_BUSINESS_CONTACT", "source_url": target.website,
            "evidence": "Contact us: info@example.com",
        }]), extracted_channels="[]", fetch_status="SUCCESS",
    ))
    session.add(snapshot)
    session.commit()
    enrich_contacts(session, limit=1)
    channel = next(item for item in target.contacts if item.channel_type)
    assert channel.channel_type == BusinessChannelType.GENERAL_BUSINESS_EMAIL
    assert channel.evidence_text == "Contact us: info@example.com"


def test_contact_form_detection_excludes_newsletter_and_job_forms():
    markup = """
    <h2>Contact Us</h2><form action="https://forms.provider.test/request">
      <input type="email" name="email"><textarea name="message"></textarea>
    </form>
    <h2>Newsletter</h2><form><input type="email" name="email"><button>Subscribe</button></form>
    <h2>Careers</h2><form><input type="email" name="email"><button>Apply Now</button></form>
    """
    rows = _channel_evidence(markup, "https://example.com/contact")
    forms = [row for row in rows if row.get("form")]
    assert len(forms) == 1
    assert forms[0]["type"] == BusinessChannelType.CONTACT_FORM
    assert forms[0]["url"] == "https://forms.provider.test/request"
    assert forms[0]["source_url"] == "https://example.com/contact"


def test_channel_ranking_idempotency_and_human_review(session):
    from outreach_agent.models import Contact, ContactEvidence

    target = company()
    session.add(target)
    session.flush()
    snapshot = WebsiteSnapshot(company_id=target.id, requested_url=target.website, fetch_status="SUCCESS")
    snapshot.pages.append(WebsitePage(
        requested_url=target.website, final_url=target.website, http_status=200, content_type="text/html",
        visible_text="Example Co", visible_text_length=10, fetch_status="SUCCESS", contact_evidence="[]",
        extracted_channels=json.dumps([
            {"email": "hello@example.com", "type": "GENERAL_BUSINESS_EMAIL", "url": target.website + "contact", "evidence": "Contact us: hello@example.com", "purpose": "Contact us"},
            {"email": "partners@example.com", "type": "PARTNERSHIP_EMAIL", "url": target.website + "about", "evidence": "Partnerships: partners@example.com", "purpose": "Partnerships"},
            {"email": "support@example.com", "type": "SUPPORT_EMAIL", "url": target.website, "evidence": "Customer support support@example.com", "purpose": "Support"},
        ]),
    ))
    session.add(snapshot)
    session.commit()
    first = enrich_contacts(session, limit=1)
    assert first.contacts_created == 3
    assert target.primary_channel.channel_type == BusinessChannelType.PARTNERSHIP_EMAIL
    assert target.contactability_status == ContactabilityStatus.READY_FOR_REVIEW
    assert sum(row.recommended for row in target.contacts) == 1
    target.primary_channel.review_status = ReviewStatus.APPROVED
    target.primary_channel.review_notes = "verified"
    session.commit()
    second = enrich_contacts(session, limit=1)
    assert second.contacts_created == 0
    assert len([row for row in target.contacts if row.channel_type]) == 3
    assert target.primary_channel.review_status == ReviewStatus.APPROVED
    assert target.primary_channel.review_notes == "verified"
    assert session.query(ContactEvidence).count() == 3


def test_external_first_party_email_is_kept_for_review(session):
    target = company()
    session.add(target)
    session.flush()
    snapshot = WebsiteSnapshot(company_id=target.id, requested_url=target.website, fetch_status="SUCCESS")
    snapshot.pages.append(WebsitePage(
        requested_url=target.website, final_url=target.website, http_status=200, content_type="text/html",
        visible_text="Example Co", visible_text_length=10, fetch_status="SUCCESS", contact_evidence="[]",
        extracted_channels=json.dumps([{
            "email": "research@gmail.com", "type": "BUSINESS_DEVELOPMENT_EMAIL", "url": "https://example.com/contact",
            "evidence": "Business inquiries: research@gmail.com", "purpose": "Business inquiries",
        }]),
    ))
    session.add(snapshot)
    session.commit()
    enrich_contacts(session, limit=1)
    assert target.primary_channel.email == "research@gmail.com"
    assert target.primary_channel.validation_status == "PUBLIC_EXTERNAL_DOMAIN"
    assert target.contactability_status == ContactabilityStatus.NEEDS_REVIEW


def test_explicit_partnership_form_outranks_general_mailbox(session):
    from outreach_agent.contacts import _recommend_primary
    from outreach_agent.models import Contact

    target = company()
    session.add(target)
    session.flush()
    general = Contact(
        company_id=target.id, email="hello@example.com", normalized_email="hello@example.com",
        contact_type=ContactType.GENERIC_BUSINESS_CONTACT, channel_type=BusinessChannelType.GENERAL_BUSINESS_EMAIL,
        source_url="https://example.com/contact", confidence="HIGH",
    )
    partnership_form = Contact(
        company_id=target.id, channel_type=BusinessChannelType.CONTACT_FORM,
        channel_url="https://forms.provider.test/partner", source_url="https://example.com/partners",
        purpose="Partnership and research collaboration inquiries", confidence="HIGH",
    )
    session.add_all([general, partnership_form])
    session.flush()
    target.contacts.extend([general, partnership_form])
    _recommend_primary(session, target)
    assert target.primary_channel.channel_type == BusinessChannelType.CONTACT_FORM


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


def test_fetch_failure_is_not_reported_as_contact_not_found(session):
    target = company()
    session.add(target)
    session.flush()
    session.add(WebsiteSnapshot(company_id=target.id, requested_url=target.website, fetch_status="FETCH_FAILED"))
    session.commit()
    summary = enrich_contacts(session, limit=1)
    assert summary.failed == 1
    assert summary.not_found == 0
    assert target.contact_status == ContactDiscoveryStatus.FAILED
