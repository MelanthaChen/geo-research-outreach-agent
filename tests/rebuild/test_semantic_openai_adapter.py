from __future__ import annotations

import json

import pytest

from outreach_v2.semantic_analysis import CONTRACT_VERSION, SupportStatus
from outreach_v2.semantic_openai import (
    CredentialUnavailable,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MODEL,
    OPENAI_ANALYSIS_SCHEMA,
    OpenAIRequestError,
    OpenAIResponsesBackend,
    PreflightError,
    build_preflight,
    build_automated_personalization,
    estimated_cost,
    reject_claims_with_unknown_evidence,
    select_evidence,
    to_evidence_contract,
    validate_openai_response,
)


def profile():
    facts = [
        {"id": "about1", "statement": "Lagoon New York is a concept store, gallery, and creation space in Brooklyn.",
         "evidence_excerpt": "Lagoon New York is a concept store, gallery, and creation space in Brooklyn.",
         "source_content": "About Lagoon: Lagoon New York is a concept store, gallery, and creation space in Brooklyn.",
         "source_urls": ["https://www.lagoonny.com/about"], "source_title": "About Lagoon",
         "section_heading": "About", "assertion_type": "ATTRIBUTED_COMPANY_DESCRIPTION", "attribution": "COMPANY_STATED"},
        {"id": "service1", "statement": "The founders offer custom design and let clients select a silhouette.",
         "evidence_excerpt": "The founders offer custom design and let clients select a silhouette.",
         "source_content": "Services: The founders offer custom design and let clients select a silhouette.",
         "source_urls": ["https://www.lagoonny.com/services"], "source_title": "Services",
         "section_heading": "Services", "assertion_type": "FIRST_PARTY_PAGE_CLAIM", "attribution": "COMPANY_STATED"},
        {"id": "event1", "statement": "Visiting artist Alice presents a gallery event.",
         "evidence_excerpt": "Visiting artist Alice presents a gallery event.",
         "source_content": "Events: Visiting artist Alice presents a gallery event.",
         "source_urls": ["https://www.lagoonny.com/events"], "source_title": "Events",
         "section_heading": "Artist event", "assertion_type": "FIRST_PARTY_PAGE_CLAIM", "attribution": "COMPANY_STATED"},
        {"id": "partner1", "statement": "Partner studio provides catering.",
         "evidence_excerpt": "Partner studio provides catering.",
         "source_content": "Partner: Partner studio provides catering.",
         "source_urls": ["https://partner.example/about"], "source_title": "Partner",
         "section_heading": "Partner", "assertion_type": "FIRST_PARTY_PAGE_CLAIM", "attribution": "THIRD_PARTY_OR_PARTNER"},
        {"id": "duplicate", "statement": "Lagoon New York is a concept store, gallery, and creation space in Brooklyn.",
         "evidence_excerpt": "Lagoon New York is a concept store, gallery, and creation space in Brooklyn.",
         "source_content": "About Lagoon: Lagoon New York is a concept store, gallery, and creation space in Brooklyn.",
         "source_urls": ["https://www.lagoonny.com/about"], "source_title": "About Lagoon",
         "section_heading": "About", "assertion_type": "FIRST_PARTY_PAGE_CLAIM", "attribution": "COMPANY_STATED"},
    ]
    return {"evidence_profile": {"pages": [
        {"url": "https://www.lagoonny.com/", "type": "home_or_other", "title": "Lagoon", "headings": [], "links": []},
        {"url": "https://www.lagoonny.com/about", "type": "about", "title": "About Lagoon", "headings": ["About"], "links": []},
        {"url": "https://www.lagoonny.com/events", "type": "events", "title": "Events", "headings": ["Alice"], "links": []},
        {"url": "https://www.lagoonny.com/services", "type": "services", "title": "Services", "headings": ["Services"], "links": []},
    ], "facts": facts}}


def response():
    return {
        "version": CONTRACT_VERSION,
        "entity_identity": {"name": "Lagoon New York", "website": "https://www.lagoonny.com/",
                            "identity_status": "INPUT_CANDIDATE", "evidence_ids": ["about1"]},
        "products_services": [{"summary": "Concept retail and custom design", "evidence_ids": ["about1", "service1"], "attribution": "COMPANY_STATED"}],
        "target_audiences": [],
        "capabilities_characteristics": [{"summary": "Custom silhouette selection", "evidence_ids": ["service1"], "attribution": "COMPANY_STATED"}],
        "proposed_claims": [
            {"id": "description", "text": "Lagoon New York combines a concept store, gallery, and creation space.", "evidence_ids": ["about1"], "attribution": "COMPANY_STATED", "role": "BUSINESS_DESCRIPTION"},
            {"id": "strength", "text": "Its custom design service lets clients begin by choosing a silhouette.", "evidence_ids": ["service1"], "attribution": "COMPANY_STATED", "role": "COMPANY_CHARACTERISTIC"},
            {"id": "relevance", "text": "The separate About and Services pages provide a case for studying how AI systems connect business identity with custom design details.", "evidence_ids": ["about1", "service1"], "attribution": "ANALYTICAL_INTERPRETATION", "role": "RESEARCH_RELEVANCE"},
        ],
        "evidence_limitations": ["Only the supplied pages were examined."],
        "observed_relationships": [{"id": "about_service", "description": "Business identity is described on About and service details appear on Services.", "evidence_ids": ["about1", "service1"], "kind": "INFORMATION_ACROSS_PAGES"}],
        "potential_geo_implications": [],
        "unsupported_or_unresolved_claims": ["The sample does not establish search performance."],
        "recommendation_decision": "ABSTAIN",
    }


def test_evidence_filter_deduplicates_and_excludes_events_and_third_parties():
    selected = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    ids = [item["id"] for item in selected.facts]
    assert ids == ["about1", "service1"]
    assert selected.classifications["about1"] == "TARGET_BUSINESS"
    assert selected.classifications["service1"] == "PRODUCT_OR_SERVICE"
    assert selected.classifications["event1"] == "EVENT_OR_PARTICIPANT"
    assert selected.excluded_reasons["partner1"] == "NON_TARGET_ATTRIBUTION:THIRD_PARTY_OR_PARTNER"
    assert selected.excluded_counts["EVENT_OR_PARTICIPANT"] == 1
    assert selected.excluded_counts["THIRD_PARTY_OR_PARTNER"] == 1


def test_quoted_first_person_review_on_service_page_is_not_company_evidence():
    source = profile()
    service_url = "https://www.lagoonny.com/services/dentistry"
    for fact_id, excerpt in (
        ("patient-review", "“I had a cracked tooth and the staff were friendly.”"),
        ("patient-review-no-pronoun", "“Great dentist and assistant; the replacement was comfortable.”"),
    ):
        source["evidence_profile"]["facts"].append({
            "id": fact_id, "statement": excerpt, "evidence_excerpt": excerpt,
            "source_content": "Reviews: " + excerpt, "source_urls": [service_url],
            "source_title": "Dental Services", "section_heading": "Dental Services",
            "assertion_type": "FIRST_PARTY_PAGE_CLAIM", "attribution": "COMPANY_STATED",
        })
    source["evidence_profile"]["facts"].append({
        "id": "unattributed-quote", "statement": "“Care made personal.”",
        "evidence_excerpt": "“Care made personal.”", "source_content": "“Care made personal.”",
        "source_urls": [service_url], "source_title": "Dental Services",
        "section_heading": "Dental Services", "assertion_type": "FIRST_PARTY_PAGE_CLAIM",
        "attribution": "COMPANY_STATED",
    })
    source["evidence_profile"]["facts"].append({
        "id": "podcast-appearance",
        "statement": "In this exclusive episode, the founder sits down with a podcast host.",
        "evidence_excerpt": "In this exclusive episode, the founder sits down with a podcast host.",
        "source_content": "In this exclusive episode, the founder sits down with a podcast host.",
        "source_urls": ["https://www.lagoonny.com/about"], "source_title": "About",
        "section_heading": "About", "assertion_type": "FIRST_PARTY_PAGE_CLAIM",
        "attribution": "COMPANY_STATED",
    })
    selected = select_evidence(source, company_name="Lagoon New York",
                               website="https://www.lagoonny.com/")
    assert selected.classifications["patient-review"] == "CUSTOMER_OR_TESTIMONIAL"
    assert selected.classifications["patient-review-no-pronoun"] == "CUSTOMER_OR_TESTIMONIAL"
    assert selected.classifications["unattributed-quote"] == "AMBIGUOUS"
    assert selected.classifications["podcast-appearance"] == "EVENT_OR_PARTICIPANT"
    selected_ids = {item["id"] for item in selected.facts}
    assert not selected_ids.intersection({
        "patient-review", "patient-review-no-pronoun", "unattributed-quote", "podcast-appearance"})
    assert selected.excluded_reasons["patient-review"] == (
        "NON_TARGET_ATTRIBUTION:CUSTOMER_OR_TESTIMONIAL")
    assert selected.excluded_reasons["unattributed-quote"] == "NON_TARGET_ATTRIBUTION:AMBIGUOUS"
    assert selected.excluded_reasons["podcast-appearance"] == (
        "NON_TARGET_ATTRIBUTION:EVENT_OR_PARTICIPANT")


def test_preflight_counts_entire_request_and_enforces_caps_and_price():
    evidence = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/").to_input()
    captured = []
    preflight, request = build_preflight(evidence, token_count=lambda text: captured.append(text) or 1000)
    assert preflight.input_tokens_estimate == 1256
    assert preflight.max_output_tokens == MAX_OUTPUT_TOKENS
    assert preflight.worst_case_cost_usd < 0.05
    assert all(fragment in captured[0] for fragment in (
        "Analyze only the supplied first-party website evidence", "json_schema", "Lagoon New York", "about1"))
    assert request["reasoning"] == {"effort": "none"}
    assert request["tools"] == [] and request["max_output_tokens"] == MAX_OUTPUT_TOKENS
    with pytest.raises(PreflightError):
        build_preflight(evidence, token_count=lambda _: MAX_INPUT_TOKENS)


def test_cost_at_maximum_authorized_usage_is_below_five_cents():
    assert estimated_cost(15_000, 6_000) == 0.03825


def test_no_credential_fails_before_constructing_or_calling_http_client(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called = False
    def factory(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("HTTP client must not be constructed without credentials")
    with pytest.raises(CredentialUnavailable):
        OpenAIResponsesBackend(client_factory=factory)
    assert not called


def test_response_requires_exact_target_identity_and_real_first_party_evidence():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    evidence = selection.to_input()
    raw = response()
    validate_openai_response(raw, evidence)
    raw["entity_identity"]["name"] = "A different business"
    with pytest.raises(ValueError, match="target business identity"):
        validate_openai_response(raw, evidence)
    raw = response()
    evidence["facts"].append({"id": "event1", "source_url": "https://www.lagoonny.com/events",
                              "classification": "EVENT_OR_PARTICIPANT", "exact_excerpt": "A visiting artist event."})
    raw["proposed_claims"][0]["evidence_ids"] = ["event1"]
    with pytest.raises(ValueError, match="non-company evidence"):
        validate_openai_response(raw, evidence)


def test_model_claim_status_is_audit_metadata_not_an_approval_queue():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    validate_openai_response(raw, selection.to_input())
    contract = to_evidence_contract(raw, selection)
    assert not contract.final_personalization_claim_ids
    assert all(item.status is SupportStatus.NOT_VERIFIABLE_FROM_SAMPLE for item in contract.claim_reviews)
    assert all(item.reviewer == "AUTOMATED_FILTERING_ONLY" for item in contract.claim_reviews)
    assert all("excluded from recipient copy" in item.rationale for item in contract.claim_reviews)
    assert "semantic entailment" in contract.claim_reviews[0].rationale


def test_model_input_does_not_expose_ids_for_excluded_facts():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    evidence = selection.to_input()
    supplied_ids = {item["id"] for item in evidence["facts"]}
    assert set(evidence["evidence_classifications"]) <= supplied_ids
    assert "event1" not in evidence["evidence_classifications"]


def test_unknown_claim_citation_is_rejected_without_discarding_other_claims():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    evidence = selection.to_input()
    raw = response()
    raw["proposed_claims"].append({
        "id": "bad-geo", "text": "The events page likely adds useful context.",
        "evidence_ids": ["missing-event-fact"], "attribution": "GEO_HYPOTHESIS",
        "role": "GEO_OBSERVATION"})
    filtered, rejected = reject_claims_with_unknown_evidence(raw, evidence)
    assert [item["id"] for item in rejected] == ["bad-geo"]
    assert rejected[0]["status"] == "REJECTED_UNKNOWN_EVIDENCE_ID"
    assert len(filtered["proposed_claims"]) == len(raw["proposed_claims"]) - 1
    validate_openai_response(filtered, evidence)


def test_invalid_evidence_id_remains_a_validation_failure_without_explicit_rejection():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    raw["proposed_claims"][0]["evidence_ids"] = ["missing-fact"]
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_openai_response(raw, selection.to_input())


def test_automated_copy_uses_grounded_source_facts_and_excludes_uncertain_model_prose():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    raw["proposed_claims"][1]["text"] = "It is not just a single-product shop."
    personalization, audit = build_automated_personalization(raw, selection, stamp="2026-10-28T00:00:00Z")
    copy = personalization["recipient_copy"]
    assert copy["business_summary"] == selection.facts[0]["text"]
    assert copy["distinctive_detail"] == selection.facts[1]["text"]
    recipient_text = " ".join(copy[key] for key in ("business_summary", "distinctive_detail", "research_relevance"))
    assert "not just" not in recipient_text.casefold()
    assert "single-product" not in recipient_text.casefold()
    assert personalization["assessment"]["claim_by_claim_approval_required"] is False
    assert personalization["assessment"]["final_invitation_review_required_before_sending"] is True
    assert next(item for item in audit if item["id"] == "strength")["status"] == "EXCLUDED_UNSUPPORTED_COMPARISON"
    assert next(item for item in audit if item["id"] == "relevance")["status"] == "RECOMPOSED_FROM_CITED_SOURCE_FACTS"
    assert personalization["assessment"]["geo_observation"] == "NOT_ESTABLISHED"


def test_single_post_request_uses_strict_json_schema_no_tools_and_no_retry_path():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    output = response()
    raw_response = {"id": "resp_test_123", "model": MODEL, "status": "completed",
                    "usage": {"input_tokens": 1234, "output_tokens": 321,
                              "output_tokens_details": {"reasoning_tokens": 0}},
                    "output_text": json.dumps(output)}
    class FakeResponse:
        status_code = 200
        def json(self): return raw_response
    class FakeClient:
        calls = []
        def __init__(self, **kwargs): self.kwargs = kwargs
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return FakeResponse()
    backend = OpenAIResponsesBackend(api_key="test-secret-not-logged", client_factory=FakeClient)
    result = backend.analyze_once(selection.to_input(), token_count=lambda _: 1000)
    assert len(FakeClient.calls) == 1
    _, call = FakeClient.calls[0]
    assert call["json"]["reasoning"]["effort"] == "none"
    assert call["json"]["tools"] == []
    assert call["json"]["text"]["format"]["strict"] is True
    assert call["json"]["text"]["format"]["schema"] == OPENAI_ANALYSIS_SCHEMA
    assert "test-secret-not-logged" not in repr(result)
    assert result["request_id"] == "resp_test_123"
    assert result["usage"]["reasoning_tokens"] == 0
    assert result["estimated_cost_usd"] == estimated_cost(1234, 321)


def test_strict_schema_marks_every_object_property_required():
    def check(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values(): check(child)
        if schema.get("type") == "array": check(schema["items"])
    check(OPENAI_ANALYSIS_SCHEMA)


def test_incomplete_api_response_is_preserved_and_never_retried():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = {"id": "resp_incomplete", "model": MODEL, "status": "incomplete",
           "usage": {"input_tokens": 100, "output_tokens": 20}, "output_text": "{}"}
    class FakeResponse:
        status_code = 200
        def json(self): return raw
    class FakeClient:
        calls = []
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return FakeResponse()
    with pytest.raises(OpenAIRequestError) as caught:
        OpenAIResponsesBackend(api_key="test", client_factory=FakeClient).analyze_once(
            selection.to_input(), token_count=lambda _: 100)
    assert caught.value.code == "INCOMPLETE_RESPONSE"
    assert caught.value.raw_response == raw
    assert len(FakeClient.calls) == 1


def test_invalid_json_or_unknown_ids_are_rejected_without_followup_request():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    raw["proposed_claims"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_openai_response(raw, selection.to_input())


def test_unsupported_numbers_and_comparatives_are_rejected():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    raw["proposed_claims"][0]["text"] = "Lagoon New York is the best concept store in Brooklyn."
    with pytest.raises(ValueError, match="unsupported comparative"):
        validate_openai_response(raw, selection.to_input())
    raw = response()
    raw["proposed_claims"][0]["text"] = "Lagoon New York has 42 stores."
    with pytest.raises(ValueError, match="unsupported numerical"):
        validate_openai_response(raw, selection.to_input())


def test_tokenizer_failure_fails_closed_before_any_request():
    evidence = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/").to_input()
    def uncertain(_): raise RuntimeError("tokenizer unavailable")
    with pytest.raises(PreflightError, match="Could not count"):
        build_preflight(evidence, token_count=uncertain)

