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


def test_high_legacy_signals_cannot_establish_eligibility():
    result = evaluate(
        {"customer_facing": True, "search_dependent": True, "company_size": "small", "geo_opportunity": "high"},
        RULES,
    )
    assert result.score == result.priority_score
    assert result.score == 0
    # A company-size label or assumed website cannot establish eligibility.
    assert result.decision == CompanyStatus.NEEDS_REVIEW
    assert result.eligibility == "NEEDS_REVIEW"


def test_missing_activity_evidence_is_not_an_ineligibility_decision():
    result = evaluate({"active": False, "dead_website": True}, RULES)
    assert result.eligibility == "NEEDS_REVIEW"
    assert result.decision == CompanyStatus.NEEDS_REVIEW


def test_reasons_are_transparent():
    result = evaluate({
        "discovery_source": True,
        "fetch_status": "SUCCESS",
        "website_evidence": {"reachable": True, "meaningful_text": True,
                             "substantial_public_information": True, "online_discovery_relevance": True,
                             "faq_page": False, "pages_inspected": 2},
    }, {**RULES, "eligibility": {"explicit_exclusions": []}, "priority": {
        "eligible": 20, "analyzable_website": 15, "online_discovery_relevance": 15,
        "geo_opportunity_high": 20, "confidence_high": 10,
        "thresholds": {"high": 75, "medium": 55},
    }})
    assert "+15 online_discovery_relevance" in result.reasons
    assert result.score == result.priority_score


def test_website_evidence_contributes_to_one_priority_score():
    rules = {**RULES, "eligibility": {"explicit_exclusions": []}, "priority": {
        "eligible": 20, "analyzable_website": 15, "online_discovery_relevance": 15,
        "geo_opportunity_high": 20, "confidence_high": 10,
        "thresholds": {"high": 75, "medium": 55},
    }}
    result = evaluate({"discovery_source": True, "fetch_status": "SUCCESS",
                       "website_evidence": {"reachable": True, "meaningful_text": True,
                                            "substantial_public_information": True,
                                            "online_discovery_relevance": True,
                                            "faq_page": False, "pages_inspected": 2}}, rules)
    assert result.score == result.priority_score == 80
    assert result.decision == CompanyStatus.QUALIFIED
    assert result.reasons == result.evidence["priority_math"]


def test_priority_separates_eligibility_opportunity_and_confidence():
    rules = dict(RULES)
    rules["priority"] = {
        "eligible": 30, "analyzable_website": 15, "smb_or_startup": 15,
        "online_discovery_relevance": 15, "geo_opportunity_high": 15,
        "geo_opportunity_medium": 8, "confidence_high": 10, "confidence_medium": 5,
        "thresholds": {"high": 75, "medium": 50},
    }
    rules["eligibility"] = {"explicit_exclusions": []}
    result = evaluate({
        "discovery_source": True,
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
    assert result.eligibility == "NEEDS_REVIEW"
    assert result.geo_opportunity == "UNKNOWN"
    assert result.priority_tier == "NEEDS_REVIEW"
    assert result.confidence == "LOW"


def test_missing_size_geography_and_industry_do_not_exclude_candidate():
    result = evaluate({
        "discovery_source": True,
        "fetch_status": "SUCCESS",
        "website_evidence": {"reachable": True, "meaningful_text": True, "pages_inspected": 2},
    }, {**RULES, "eligibility": {"explicit_exclusions": []}})
    assert result.eligibility == "ELIGIBLE"
    assert result.priority_tier in {"HIGH", "MEDIUM", "LOW"}


def test_explicit_configured_exclusion_required_for_ineligible():
    signals = {"discovery_source": True, "ineligible": True,
               "website_evidence": {"reachable": True, "meaningful_text": True, "pages_inspected": 2}}
    assert evaluate(signals, {**RULES, "eligibility": {"explicit_exclusions": []}}).eligibility == "ELIGIBLE"
    result = evaluate(signals, {**RULES, "eligibility": {"explicit_exclusions": ["ineligible"]}})
    assert result.eligibility == "INELIGIBLE"


def test_explicit_inactive_evidence_needs_review_not_silent_assumption():
    result = evaluate({"discovery_source": True, "active": False,
                       "website_evidence": {"reachable": True, "meaningful_text": True, "pages_inspected": 2}},
                      {**RULES, "eligibility": {"explicit_exclusions": []}})
    assert result.eligibility == "NEEDS_REVIEW"


def test_geo_opportunity_does_not_claim_observed_ai_visibility():
    result = evaluate({
        "discovery_source": True, "fetch_status": "SUCCESS",
        "website_evidence": {"reachable": True, "meaningful_text": True,
                             "substantial_public_information": True, "online_discovery_relevance": True,
                             "faq_page": False, "pages_inspected": 2},
    }, {**RULES, "eligibility": {"explicit_exclusions": []}})
    assert result.geo_opportunity == "HIGH"
    assert "AI visibility" not in "; ".join(result.geo_reasons or [])


def test_contactability_does_not_change_geo_opportunity_or_priority():
    base = {"discovery_source": True, "fetch_status": "SUCCESS",
            "website_evidence": {"reachable": True, "meaningful_text": True,
                                 "substantial_public_information": True, "online_discovery_relevance": True,
                                 "faq_page": False, "pages_inspected": 2}}
    first = evaluate(base, {**RULES, "eligibility": {"explicit_exclusions": []}})
    second = evaluate({**base, "contactability_status": "FETCH_FAILED"}, {**RULES, "eligibility": {"explicit_exclusions": []}})
    assert (first.geo_opportunity, first.priority_score) == (second.geo_opportunity, second.priority_score)


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
