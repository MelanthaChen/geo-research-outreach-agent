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
