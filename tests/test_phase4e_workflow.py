import json
from pathlib import Path

from outreach_agent.models import Company, CompanyStatus, DiscoveryRecord, ReviewStatus, WebsitePage, WebsiteSnapshot
from outreach_agent.qualification import qualify_companies


def test_scoped_qualification_preserves_existing_company_decisions(session):
    baseline = Company(
        company_name="Baseline Co", normalized_name="baseline co", website="https://baseline.example",
        normalized_domain="baseline.example", eligibility="ELIGIBLE", geo_opportunity="HIGH",
        priority_score=80, priority_tier="HIGH", evidence_confidence="HIGH",
        review_status=ReviewStatus.APPROVED, review_notes="Keep human decision",
        pipeline_status=CompanyStatus.QUALIFIED,
    )
    new = Company(
        company_name="New Co", normalized_name="new co", website="https://new.example",
        normalized_domain="new.example", eligibility="NEEDS_REVIEW", geo_opportunity="UNKNOWN",
        priority_tier="NEEDS_REVIEW", review_status=ReviewStatus.PENDING,
        pipeline_status=CompanyStatus.NEEDS_REVIEW,
    )
    session.add_all([baseline, new])
    session.flush()
    session.add(DiscoveryRecord(company_id=new.id, source_type="test", source_identifier="new-1", raw_data="{}"))
    page = WebsitePage(
        requested_url=new.website, final_url=new.website, http_status=200, content_type="text/html",
        title="New Co company", visible_text="New Co offers services. " * 80,
        visible_text_length=2000, structured_data_types="[]", contact_evidence="[]",
        extracted_channels="[]", fetch_status="SUCCESS",
    )
    session.add(WebsiteSnapshot(
        company_id=new.id, requested_url=new.website, final_url=new.website, http_status=200,
        fetch_status="SUCCESS", extracted_signals=json.dumps({
            "reachable": True, "meaningful_text": True, "substantial_public_information": True,
            "online_discovery_relevance": True, "faq_page": False, "structured_data": False,
            "pages_inspected": 1,
        }), pages=[page],
    ))
    session.commit()

    result = qualify_companies(session, Path("config/qualification.yaml"), force=True, company_ids=[new.id])

    assert result["processed"] == 1
    session.refresh(baseline)
    session.refresh(new)
    assert (baseline.eligibility, baseline.geo_opportunity, baseline.priority_tier, baseline.priority_score) == ("ELIGIBLE", "HIGH", "HIGH", 80)
    assert baseline.review_status == ReviewStatus.APPROVED
    assert baseline.review_notes == "Keep human decision"
    assert new.eligibility == "ELIGIBLE"
