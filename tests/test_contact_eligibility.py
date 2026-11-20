import pytest

from outreach_agent.contact_eligibility import eligible_business_email_channels


def _record(contact_state="UNKNOWN", *, review_state="PENDING", source="https://shop.org/contact",
            evidence="For general business inquiries, email hello@shop.org.", purpose=""):
    return {
        "identity_verification_status": "VERIFIED",
        "contact_verification_status": contact_state,
        "official_website": "https://shop.org",
        "suppression_status": "NOT_SUPPRESSED",
        "contact_channels": [{
            "channel_type": "GENERAL_BUSINESS_EMAIL",
            "email": "hello@shop.org",
            "source_url": source,
            "evidence": evidence,
            "purpose": purpose,
            "review_status": review_state,
        }],
    }


@pytest.mark.parametrize("contact_state,channel_state", [
    ("NOT_VERIFIED", "PENDING"), ("CONTACT_EVIDENCE_PRESENT", "UNKNOWN"), ("CONTACT_VERIFIED", "STALE_VERIFIED"),
])
def test_contact_freshness_or_review_state_does_not_gate_first_party_email(contact_state, channel_state):
    record = _record(contact_state)
    record["contact_channels"][0]["verification_status"] = channel_state
    assert [item["email"] for item in eligible_business_email_channels(record)] == ["hello@shop.org"]


@pytest.mark.parametrize("field,value", [
    ("review_status", "REJECTED"), ("review_status", "SUPPRESSED"),
    ("verification_status", "INVALID"), ("email_verification_status", "REJECTED"),
    ("suppression_status", "SUPPRESSED"), ("status", "DO_NOT_USE"),
])
def test_explicitly_blocked_contact_state_stays_ineligible(field, value):
    record = _record()
    record["contact_channels"][0][field] = value
    assert eligible_business_email_channels(record) == []


@pytest.mark.parametrize("record", [
    _record(source="https://directory.example/contact"),
    _record(evidence="For general business inquiries, email hello@other.example."),
    _record(evidence="Email hello@shop.org for support and help desk issues"),
    _record(evidence="General questions are welcome; no email address shown."),
])
def test_email_requires_exact_official_evidence_and_appropriate_purpose(record):
    assert eligible_business_email_channels(record) == []


def test_subdomain_is_first_party_and_status_timestamp_is_irrelevant():
    record = _record(contact_state="NOT_VERIFIED", source="https://www.shop.org/contact")
    record["contact_channels"][0]["verified_at"] = None
    assert len(eligible_business_email_channels(record)) == 1


def test_company_level_suppression_blocks_saved_evidence():
    record = _record()
    record["suppression_status"] = "SUPPRESSED"
    assert eligible_business_email_channels(record) == []


@pytest.mark.parametrize("purpose", ["Experience the Difference", "", None])
def test_schema_org_email_on_official_contact_page_is_general_without_prose(purpose):
    email = "informationemail@1stsummit.bank"
    record = _record(source="https://www.1stsummit.bank/contact-us/", purpose=purpose or "")
    record["official_website"] = "https://www.1stsummit.bank/"
    record["contact_channels"][0].update({
        "channel_type": "GENERAL_BUSINESS_EMAIL",
        "email": email,
        "verification_status": "UNKNOWN",
        "evidence": '{"@type": "Organization", "name": "1st Summit Bank", '
                    '"description": "Experience the Difference", "url": '
                    '"https://www.1stsummit.bank/", "email": "informationemail@1stsummit.bank"}',
        "purpose": purpose,
    })
    assert [item["email"] for item in eligible_business_email_channels(record)] == [email]


def test_schema_org_email_does_not_override_restriction_or_non_contact_route():
    email = "informationemail@1stsummit.bank"
    record = _record(source="https://www.1stsummit.bank/privacy/", purpose="Experience the Difference")
    record["official_website"] = "https://www.1stsummit.bank/"
    record["contact_channels"][0].update({
        "channel_type": "GENERAL_BUSINESS_EMAIL", "email": email,
        "evidence": '{"@type":"Organization","email":"informationemail@1stsummit.bank"}',
    })
    assert eligible_business_email_channels(record) == []

    record["contact_channels"][0]["source_url"] = "https://www.1stsummit.bank/contact-us/"
    record["contact_channels"][0]["email"] = "support@1stsummit.bank"
    record["contact_channels"][0]["evidence"] = '{"@type":"Organization","email":"support@1stsummit.bank"}'
    assert eligible_business_email_channels(record) == []

    record["contact_channels"][0]["email"] = email
    record["contact_channels"][0]["evidence"] = (
        '{"@type":"Organization","description":"For customer support, email '
        'informationemail@1stsummit.bank","email":"informationemail@1stsummit.bank"}')
    assert eligible_business_email_channels(record) == []


def test_url_saved_as_contact_value_is_not_an_email():
    record = _record()
    record["contact_channels"][0]["email"] = "https://www.1stsummit.bank/contact-us/"
    record["contact_channels"][0]["evidence"] = '{"@type":"Organization","email":"https://www.1stsummit.bank/contact-us/"}'
    assert eligible_business_email_channels(record) == []
