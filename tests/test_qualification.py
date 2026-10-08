from outreach_agent.models import Company, CompanyStatus, transition_company
from outreach_agent.qualification import evaluate


RULES = {
    "thresholds": {"qualified": 55, "needs_review": 30},
    "weights": {
        "has_website": 25,
        "customer_facing": 20,
        "search_dependent": 20,
        "content_rich": 15,
        "smb_or_startup": 20,
        "active": 10,
        "geo_opportunity": 15,
    },
    "penalties": {"dead_website": -60, "unsuitable": -50, "large_enterprise": -25},
}


def test_qualified_company():
    result = evaluate(
        {"customer_facing": True, "search_dependent": True, "company_size": "small", "geo_opportunity": "high"},
        RULES,
    )
    assert result.score == 100
    assert result.decision == CompanyStatus.QUALIFIED


def test_dead_site_is_rejected():
    result = evaluate({"active": False, "dead_website": True}, RULES)
    assert result.score == 0
    assert result.decision == CompanyStatus.REJECTED


def test_reasons_are_transparent():
    result = evaluate({"content_rich": True, "active": True}, RULES)
    assert "+15 content_rich" in result.reasons


def test_website_evidence_contributes_explainable_score():
    rules = dict(RULES)
    rules["website_weights"] = {"reachable": 15, "faq_page": 5}
    rules["website_penalties"] = {"fetch_failed": -40}
    result = evaluate({"active": False, "website_evidence": {"reachable": True, "faq_page": True}}, rules)
    assert result.score == 45
    assert result.decision == CompanyStatus.NEEDS_REVIEW
    assert "+15 website:reachable" in result.reasons


def test_priority_separates_eligibility_opportunity_and_confidence():
    rules = dict(RULES)
    rules["priority"] = {
        "eligible": 30, "analyzable_website": 15, "smb_or_startup": 15,
        "online_discovery_relevance": 15, "geo_opportunity_high": 15,
        "geo_opportunity_medium": 8, "confidence_high": 10, "confidence_medium": 5,
        "thresholds": {"high": 75, "medium": 50},
    }
    result = evaluate({
        "company_size_category": "SMALL", "fetch_status": "SUCCESS", "visible_text_length": 2500,
        "website_evidence": {
            "reachable": True, "meaningful_text": True, "substantial_public_information": True,
            "online_discovery_relevance": True, "faq_page": False, "structured_data": False,
            "pages_inspected": 3,
        },
    }, rules)
    assert result.eligibility == "ELIGIBLE"
    assert result.geo_opportunity == "HIGH"
    assert result.priority_tier == "HIGH"
    assert result.confidence == "HIGH"
    assert result.evidence["priority_math"]


def test_missing_evidence_is_unknown_not_low_opportunity():
    rules = dict(RULES)
    rules["priority"] = {"thresholds": {"high": 75, "medium": 50}}
    result = evaluate({"fetch_status": "BLOCKED", "website_evidence": {"blocked": True}}, rules)
    assert result.eligibility == "UNKNOWN"
    assert result.geo_opportunity == "UNKNOWN"
    assert result.priority_tier == "NEEDS_REVIEW"
    assert result.confidence == "LOW"


def test_company_status_transitions_are_guarded():
    company = Company(
        company_name="Example",
        normalized_name="example",
        website="https://example.example",
        normalized_domain="example.example",
    )
    company.pipeline_status = CompanyStatus.DISCOVERED
    transition_company(company, CompanyStatus.QUALIFIED)
    transition_company(company, CompanyStatus.READY_FOR_GEO)
    try:
        transition_company(company, CompanyStatus.DISCOVERED)
    except ValueError as exc:
        assert "READY_FOR_GEO -> DISCOVERED" in str(exc)
    else:
        raise AssertionError("invalid transition was accepted")
