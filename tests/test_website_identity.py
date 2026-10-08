import json

import pytest

from outreach_agent.models import Company, WebsitePage, WebsiteSnapshot
from outreach_agent.website_identity import verify_company_website


def company(name="Acme Analytics", website="https://acme-analytics.example"):
    return Company(company_name=name, normalized_name=name.casefold(), website=website, normalized_domain=website.split("//", 1)[-1])


def snapshot(*, status="SUCCESS", final_url="https://acme-analytics.example/", title="Acme Analytics | Company", text="Acme Analytics builds analytics software. " * 30, http_status=200, error=None):
    page = WebsitePage(
        requested_url="https://acme-analytics.example/", final_url=final_url, http_status=http_status,
        content_type="text/html", title=title, meta_description=None, visible_text=text,
        visible_text_length=len(text), structured_data_types="[]", contact_evidence="[]",
        extracted_channels="[]", fetch_status="SUCCESS" if status == "SUCCESS" else status,
    )
    return WebsiteSnapshot(
        company_id=1, requested_url="https://acme-analytics.example/", final_url=final_url,
        http_status=http_status, fetch_status=status, error_message=error,
        extracted_signals=json.dumps({"meaningful_text": len(text) >= 500}), pages=[page],
    )


def test_identity_verification_requires_domain_and_content_evidence():
    result = verify_company_website(company(), snapshot())
    assert result.status == "VERIFIED"
    assert "acme" in result.identity_tokens
    assert result.usable_content is True


def test_http_200_alone_does_not_verify_identity():
    result = verify_company_website(company(), snapshot(title="Digital Marketing Platform", text="Digital marketing services for businesses. " * 30))
    assert result.status == "AMBIGUOUS_IDENTITY"


def test_third_party_profile_is_rejected_without_fetch_evidence():
    result = verify_company_website(company("Example", "https://www.linkedin.com/in/example"), None)
    assert result.status == "THIRD_PARTY_URL"


def test_cross_domain_redirect_without_identity_evidence_is_mismatch():
    result = verify_company_website(
        company(), snapshot(final_url="https://unrelated.example/", title="Online Casino", text="Sports betting casino bonus " * 30),
    )
    assert result.status == "REJECTED_MISMATCH"


def test_parked_domain_is_rejected_as_mismatch():
    result = verify_company_website(company(), snapshot(title="Domain for sale", text="This domain is for sale. " * 30))
    assert result.status == "REJECTED_MISMATCH"


def test_blocked_and_robots_denied_remain_separate_from_mismatch():
    assert verify_company_website(company(), snapshot(status="BLOCKED", http_status=403)).status == "ACCESS_BLOCKED"
    assert verify_company_website(company(), snapshot(status="ROBOTS_DENIED")).status == "ROBOTS_DENIED"


def test_fetch_failure_is_not_identity_mismatch():
    assert verify_company_website(company(), snapshot(status="FETCH_FAILED", error="connection timeout")).status == "FETCH_FAILED"


@pytest.mark.parametrize("website", ["https://linkedin.com/company/acme", "https://instagram.com/acme", "https://www.tiktok.com/@acme"])
def test_common_social_profiles_are_third_party_urls(website):
    assert verify_company_website(company("Acme", website), None).status == "THIRD_PARTY_URL"
