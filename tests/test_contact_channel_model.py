from outreach_agent.contact_channel_model import classify_contact_channels


def test_company_channel_classes_distinguish_email_form_both_and_missing():
    email = {"channel_type": "GENERAL_BUSINESS_EMAIL", "email": "info@company.test", "is_suitable_first_party": True}
    form = {"channel_type": "CONTACT_FORM", "channel_url": "https://company.test/contact", "is_suitable_first_party": True}
    assert classify_contact_channels([email]) == "EMAIL_ONLY"
    assert classify_contact_channels([form]) == "FORM_ONLY"
    assert classify_contact_channels([email, form]) == "EMAIL_AND_FORM"
    assert classify_contact_channels([]) == "NO_CONTACT_CHANNEL"


def test_unusable_unrejected_evidence_is_needs_review_not_missing():
    unresolved = {"channel_type": "CONTACT_PAGE", "channel_url": "https://company.test/contact", "review_status": "PENDING", "is_suitable_first_party": False}
    rejected = unresolved | {"review_status": "REJECTED"}
    assert classify_contact_channels([unresolved]) == "NEEDS_REVIEW"
    assert classify_contact_channels([rejected]) == "NO_CONTACT_CHANNEL"
