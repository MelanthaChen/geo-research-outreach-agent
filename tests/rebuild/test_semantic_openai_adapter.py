from __future__ import annotations

import json
from pathlib import Path

import pytest

from outreach_v2.semantic_analysis import CONTRACT_VERSION, SupportStatus
from outreach_v2.semantic_openai import (
    CredentialUnavailable,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MODEL,
    OPENAI_ANALYSIS_SCHEMA,
    SYSTEM_INSTRUCTION,
    OpenAIRequestError,
    OpenAIResponsesBackend,
    PreflightError,
    EvidenceSelection,
    build_preflight,
    build_automated_personalization,
    estimated_cost,
    replay_saved_provider_attempt,
    reject_claims_with_unknown_evidence,
    select_evidence,
    to_evidence_contract,
    validate_openai_response,
    attempt_failure_accounting,
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
        "products_services": [{"summary": "Concept retail paired with custom design for clients", "evidence_ids": ["about1", "service1"], "attribution": "COMPANY_STATED"}],
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
    assert all("not prove semantic entailment" in item.rationale for item in contract.claim_reviews)
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


def test_invalid_structured_citation_and_unsupported_claim_are_removed_individually():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    evidence = selection.to_input()
    raw = response()
    raw["products_services"].append({"summary": "An unsupported service claim",
        "evidence_ids": ["missing-service-id"], "attribution": "COMPANY_STATED"})
    raw["proposed_claims"].append({"id": "bad-number", "text": "Lagoon New York has 42 stores.",
        "evidence_ids": ["about1"], "attribution": "COMPANY_STATED", "role": "BUSINESS_DESCRIPTION"})
    filtered, rejected = reject_claims_with_unknown_evidence(raw, evidence)
    assert {item["entry_type"] for item in rejected} == {"PRODUCT_OR_SERVICE", "PROPOSED_CLAIM"}
    by_type = {item["entry_type"]: item for item in rejected}
    assert by_type["PRODUCT_OR_SERVICE"]["status"] == "REJECTED_UNKNOWN_EVIDENCE_ID"
    assert by_type["PROPOSED_CLAIM"]["id"] == "bad-number"
    assert by_type["PROPOSED_CLAIM"]["status"] == "REJECTED_UNSUPPORTED_NUMERICAL_CLAIM"
    assert len(filtered["products_services"]) == 1
    assert all(item["id"] != "bad-number" for item in filtered["proposed_claims"])
    validate_openai_response(filtered, evidence)


def test_lagoon_archived_response_replay_keeps_supported_claims_and_grounded_copy():
    root = Path(__file__).parents[2] / "src/outreach_v2/demo_data"
    run = json.loads((root / "lagoon-openai-run.json").read_text())
    replay = json.loads((root / "lagoon-openai-replay.json").read_text())
    evidence = run["selected_evidence"]
    raw = run["raw_structured_model_response"]
    output_text = raw.get("output_text") or "".join(
        content.get("text", "")
        for item in raw.get("output", [])
        for content in item.get("content", [])
        if content.get("type") == "output_text")
    structured = json.loads(output_text)
    filtered, rejected = reject_claims_with_unknown_evidence(structured, evidence)
    validate_openai_response(filtered, evidence)
    assert [claim["id"] for claim in filtered["proposed_claims"]] == replay["accepted_claim_ids"]
    assert next(item for item in rejected if item["id"] == "c4")["unknown_evidence_ids"] == ["p2_jsonld_0"]
    # The saved Lagoon request and deployed semantic adapter use the exact same
    # system instruction; the workflow is reused rather than redesigned.
    assert run["request_payload_without_credentials"]["instructions"] == SYSTEM_INSTRUCTION

    selection = EvidenceSelection(evidence["target_company"], evidence["website"],
        tuple(evidence["pages"]), tuple(evidence["facts"]),
        evidence["evidence_classifications"], evidence["excluded_reasons"],
        evidence["excluded_counts"], tuple(evidence["limitations"]))
    personalization, audit = build_automated_personalization(
        filtered, selection, stamp=run["fetched_at"])
    copy = personalization["recipient_copy"]
    assert copy["business_summary"]
    assert copy["distinctive_detail"]
    assert copy["research_relevance"]
    assert len({copy[key] for key in ("business_summary", "distinctive_detail", "research_relevance")}) == 3
    fact_ids = {item["id"] for item in selection.facts}
    assert set(copy["support"]["business_summary"] + copy["support"]["distinctive_detail"]
               + copy["support"]["research_relevance"]) <= fact_ids
    assert any(item["id"] == "c2" and item["status"] == "EXCLUDED_UNSUPPORTED_COMPARISON"
               for item in audit)


def test_lagoon_known_cost_validation_failure_replays_offline_without_new_request():
    root = Path(__file__).parents[2] / "src/outreach_v2/demo_data"
    run = json.loads((root / "lagoon-openai-run.json").read_text())
    evidence = run["selected_evidence"]
    raw = run["raw_structured_model_response"]
    cost = run["model_result"]["estimated_cost_usd"]
    usage = raw["usage"]
    diagnostic = {"diagnostic_version": 1, "http_status": 200,
        "request_id": raw["id"], "model": raw["model"], "response_status": raw["status"],
        "usage": usage, "actual_cost_usd": cost, "failure_code": "STRUCTURED_VALIDATION_FAILED",
        "failure_stage": "evidence_reference_validation", "failure_path": "Claim c4",
        "provider_response": raw}
    saved = {"status": "VALIDATION_FAILED", "provider_response": json.dumps(diagnostic),
        "actual_cost_usd": str(cost), "request_id": raw["id"]}
    preflight, _ = build_preflight(evidence, token_count=lambda _: 1000)
    result = replay_saved_provider_attempt(saved, evidence, preflight)
    assert result["offline_replay"] is True
    assert result["request_id"] == raw["id"]
    assert result["estimated_cost_usd"] == cost
    assert result["usage"]["input_tokens"] == usage["input_tokens"]
    assert [item["id"] for item in result["rejected_claims"] if item["id"]] == ["c4"]


def test_completed_response_nested_in_diagnostic_envelope_replays_offline():
    evidence = select_evidence(profile(), company_name="Lagoon New York",
                               website="https://www.lagoonny.com/").to_input()
    provider = {"id": "resp_nested_replay", "model": MODEL, "status": "completed",
        "usage": {"input_tokens": 1000, "output_tokens": 400,
                  "output_tokens_details": {"reasoning_tokens": 0}},
        "output_text": json.dumps(response())}
    cost = estimated_cost(1000, 400)
    saved = {"status": "COMPLETED", "request_id": provider["id"],
        "actual_cost_usd": str(cost),
        "provider_response": json.dumps({"usage": provider["usage"],
            "actual_cost_usd": cost, "provider_response": provider})}
    preflight, _ = build_preflight(evidence, token_count=lambda _: 1000)
    replay = replay_saved_provider_attempt(saved, evidence, preflight)
    assert replay["offline_replay"] is True
    assert replay["request_id"] == provider["id"]
    assert replay["estimated_cost_usd"] == cost


def test_explicit_abstain_discards_geo_hypotheses_without_rejecting_factual_copy():
    selection = select_evidence(profile(), company_name="Lagoon New York",
                                website="https://www.lagoonny.com/")
    raw = response()
    raw["potential_geo_implications"] = [{"id": "geo1", "observation": "A page relationship",
        "hypothesis": "A possible GEO interpretation", "recommendation": "Change a page",
        "evidence_ids": ["about1", "service1"], "relationship_ids": ["about_service"],
        "status": "HYPOTHESIS_REQUIRES_REVIEW"}]
    normalized, audit = build_automated_personalization(raw, selection, stamp="2026-11-12T00:00:00Z")
    assert normalized["recommendations"] == []
    assert normalized["assessment"]["geo_observation"] == "NOT_ESTABLISHED"
    assert normalized["recipient_copy"]["business_summary"]
    assert normalized["assessment"]["excluded_model_recommendations"][0]["id"] == "geo1"
    assert any(item["status"] == "EXCLUDED_INCONSISTENT_WITH_ABSTAIN" for item in audit)


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
    assert copy["business_summary"] == "Lagoon New York combines a concept store, gallery, and creation space."
    assert copy["distinctive_detail"] == "Concept retail paired with custom design for clients"
    recipient_text = " ".join(copy[key] for key in ("business_summary", "distinctive_detail", "research_relevance"))
    assert "not just" not in recipient_text.casefold()
    assert "single-product" not in recipient_text.casefold()
    assert personalization["assessment"]["claim_by_claim_approval_required"] is False
    assert personalization["assessment"]["final_invitation_review_required_before_sending"] is True
    assert next(item for item in audit if item["id"] == "strength")["status"] == "EXCLUDED_UNSUPPORTED_COMPARISON"
    assert next(item for item in audit if item["id"] == "relevance")["status"] == "USED_IN_RECIPIENT_COPY_WITH_EVIDENCE_REFERENCES"
    assert personalization["assessment"]["geo_observation"] == "NOT_ESTABLISHED"


def test_long_first_party_excerpts_use_cited_semantic_copy_without_copying_source_text():
    from outreach_v2.quality import quality_decisions
    source = profile()
    long_about = ("Lagoon New York is a Brooklyn concept store, gallery, and creation space "
        "that brings together wearable art, curated housewares, jewelry, and community programming "
        "in a setting connected to sustainability and the natural environment.")
    long_service = ("The founders offer custom design and let clients select a silhouette, size, "
        "fabric, and colors for made-to-order garments developed for individual projects, "
        "including looks prepared to meet the needs of a particular event or production, "
        "plus fittings and finishing services for those garments.")
    source["evidence_profile"]["facts"][0].update(statement=long_about, evidence_excerpt=long_about,
        source_content="About: " + long_about)
    source["evidence_profile"]["facts"][1].update(statement=long_service, evidence_excerpt=long_service,
        source_content="Services: " + long_service)
    selection = select_evidence(source, company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    raw["products_services"][0]["summary"] = (
        "Made-to-order custom design lets clients choose garment details for their project.")
    personalization, _ = build_automated_personalization(raw, selection, stamp="2026-11-12T00:00:00Z")
    copy = personalization["recipient_copy"]
    result = quality_decisions(personalization, record={"fixture": False},
        candidate_name="Lagoon New York", website="https://www.lagoonny.com/")
    assert len(long_about) > 180 and len(long_service) > 260
    assert copy["business_summary"] != long_about
    assert copy["distinctive_detail"] != long_service
    assert copy["support"]["business_summary"] == ["about1"]
    assert copy["support"]["distinctive_detail"] == ["about1", "service1"]
    assert result["OUTREACH_REVIEW_READY"]["passed"] is True


def test_recipient_summary_uses_cited_semantic_claim_when_it_does_not_repeat_identity_fact_id():
    from outreach_v2.quality import quality_decisions
    selection = select_evidence(profile(), company_name="Lagoon New York",
                                website="https://www.lagoonny.com/")
    raw = response()
    raw["proposed_claims"][0].update(
        text="Lagoon New York offers custom design that lets clients select a silhouette.",
        evidence_ids=["service1"])
    personalization, _ = build_automated_personalization(
        raw, selection, stamp="2026-11-12T00:00:00Z")
    copy_result = quality_decisions(personalization, record={"fixture": False},
        candidate_name="Lagoon New York", website="https://www.lagoonny.com/")
    assert personalization["recipient_copy"]["business_summary"] == raw["proposed_claims"][0]["text"]
    assert personalization["recipient_copy"]["support"]["business_summary"] == ["service1"]
    assert "SOURCE_TEXT_COPIED_TO_RECIPIENT_COPY" not in copy_result["OUTREACH_REVIEW_READY"]["reasons"]


def test_structured_first_party_offerings_are_not_misclassified_as_ambiguous():
    source = profile()
    source["evidence_profile"]["facts"].append({
        "id": "hotel-amenities", "statement": "The property offers guest rooms and a seasonal pool.",
        "evidence_excerpt": "The property offers guest rooms and a seasonal pool.",
        "source_content": "Hotel data: The property offers guest rooms and a seasonal pool.",
        "source_urls": ["https://www.lagoonny.com/about"], "source_title": "Property details",
        "section_heading": "Property details", "assertion_type": "STRUCTURED_FIRST_PARTY_DATA",
        "attribution": "PUBLISHED_STRUCTURED_DATA", "field": "amenityFeature",
    })
    selected = select_evidence(source, company_name="Lagoon New York", website="https://lagoonny.com/")
    assert selected.classifications["hotel-amenities"] == "PRODUCT_OR_SERVICE"


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


def test_first_party_is_not_mistaken_for_unsupported_superlative():
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    raw = response()
    raw["proposed_claims"][0]["text"] = "Lagoon New York's first-party website describes its concept store."
    assert validate_openai_response(raw, selection.to_input()) is None

    raw = response()
    raw["proposed_claims"][0]["text"] = "Lagoon New York is the first provider in Brooklyn."
    with pytest.raises(ValueError, match="unsupported comparative"):
        validate_openai_response(raw, selection.to_input())


def test_prompt_is_identical_to_the_lagoon_semantic_workflow():
    run = json.loads((Path(__file__).parents[2] / "src/outreach_v2/demo_data/lagoon-openai-run.json").read_text())
    assert run["request_payload_without_credentials"]["instructions"] == SYSTEM_INSTRUCTION


def test_tokenizer_failure_fails_closed_before_any_request():
    evidence = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/").to_input()
    def uncertain(_): raise RuntimeError("tokenizer unavailable")
    with pytest.raises(PreflightError, match="Could not count"):
        build_preflight(evidence, token_count=uncertain)


def _run_fake_response(raw, *, status=200):
    selection = select_evidence(profile(), company_name="Lagoon New York", website="https://www.lagoonny.com/")
    class FakeResponse:
        status_code = status
        headers = {"x-request-id": "resp_header_diag"}
        text = json.dumps(raw) if isinstance(raw, (dict, list)) else str(raw)
        def json(self):
            if not isinstance(raw, (dict, list)):
                raise ValueError("malformed provider body")
            return raw
    class FakeClient:
        calls = 0
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, *args, **kwargs):
            self.calls += 1
            return FakeResponse()
    client = FakeClient()
    backend = OpenAIResponsesBackend(api_key="test-only", client_factory=lambda **kwargs: client)
    try:
        result = backend.analyze_once(selection.to_input(), token_count=lambda _: 100)
        return result, client.calls
    except OpenAIRequestError as exc:
        return exc, client.calls


def _provider_response(output):
    return {"id": "resp_diag_123", "model": MODEL, "status": "completed",
            "finish_reason": "stop",
            "usage": {"input_tokens": 1234, "output_tokens": 321,
                      "output_tokens_details": {"reasoning_tokens": 0}},
            "output_text": json.dumps(output)}


def test_malformed_structured_json_preserves_provider_metadata_and_known_cost():
    raw = {"id": "resp_badjson", "model": MODEL, "status": "completed", "finish_reason": "stop",
           "usage": {"input_tokens": 100, "output_tokens": 20}, "output_text": "{bad"}
    error, calls = _run_fake_response(raw)
    assert calls == 1
    assert error.code == "INVALID_STRUCTURED_OUTPUT"
    assert error.failure_stage == "structured_json_parse"
    assert error.failure_path == "$"
    assert error.request_id == "resp_badjson"
    assert error.usage["input_tokens"] == 100
    assert error.actual_cost_usd == estimated_cost(100, 20)
    artifact = error.diagnostic_artifact()
    assert artifact["provider_response"] == raw
    assert artifact["response_status"] == "completed"
    assert artifact["finish_reason"] == "stop"


def test_malformed_provider_json_body_is_distinguished_and_bounded_for_diagnostics():
    error, calls = _run_fake_response("not-json")
    assert calls == 1
    assert error.code == "INVALID_RESPONSE_JSON"
    assert error.failure_stage == "provider_response_json"
    assert error.request_id == "resp_header_diag"
    assert error.diagnostic_artifact()["provider_response"]["body_text"] == "not-json"


def test_schema_mismatch_reports_exact_validation_path_and_preserves_response():
    output = response()
    del output["entity_identity"]["website"]
    raw = _provider_response(output)
    error, calls = _run_fake_response(raw)
    assert calls == 1
    assert error.code == "STRUCTURED_VALIDATION_FAILED"
    assert error.failure_stage == "json_schema_validation"
    assert error.failure_path == "$.entity_identity"
    assert error.request_id == "resp_diag_123"
    assert error.actual_cost_usd == estimated_cost(1234, 321)
    assert error.diagnostic_artifact()["provider_response"] == raw


def test_invalid_evidence_reference_is_distinguished_from_schema_failure():
    output = response()
    output["products_services"][0]["evidence_ids"] = ["missing-source-id"]
    result, calls = _run_fake_response(_provider_response(output))
    assert calls == 1
    assert isinstance(result, dict)
    assert result["rejected_claims"][0]["entry_type"] == "PRODUCT_OR_SERVICE"
    assert result["rejected_claims"][0]["unknown_evidence_ids"] == ["missing-source-id"]
    assert result["structured_output"]["products_services"] == []


def test_provider_success_local_identity_failure_has_replayable_response_and_usage():
    output = response()
    output["entity_identity"]["name"] = "Different business"
    raw = _provider_response(output)
    error, calls = _run_fake_response(raw)
    assert calls == 1
    assert error.failure_stage == "identity_validation"
    assert error.actual_cost_usd == estimated_cost(1234, 321)
    artifact = error.diagnostic_artifact()
    assert artifact["failure_message"] == "Model changed the target business identity"
    assert artifact["provider_response"] == raw
    assert artifact["usage"] == raw["usage"]


def test_diagnostic_artifact_redacts_key_shapes_and_has_bounded_response_size():
    error = OpenAIRequestError(200, "STRUCTURED_VALIDATION_FAILED", "bad sk-12345678901234567890",
                               raw_response={"output_text": "sk-12345678901234567890"},
                               failure_stage="json_schema_validation")
    artifact = error.diagnostic_artifact()
    assert "sk-12345678901234567890" not in json.dumps(artifact)
    assert "[REDACTED]" in json.dumps(artifact)


def test_attempt_accounting_keeps_unknown_reservation_and_records_verified_cost():
    unknown = OpenAIRequestError(200, "USAGE_MISSING", "missing usage", raw_response={"id": "resp_unknown"})
    prior = {"reserved_usd": "0.02985", "actual_cost_usd": "0", "usage": "{}"}
    unknown_values = attempt_failure_accounting(unknown, prior)
    assert unknown_values["status"] == "UNKNOWN_SPEND"
    assert "reserved_usd" not in unknown_values
    assert unknown_values["actual_cost_usd"] == "0"

    known = OpenAIRequestError(200, "STRUCTURED_VALIDATION_FAILED", "invalid identity",
                               raw_response={"id": "resp_known"}, usage={"input_tokens": 10, "output_tokens": 5},
                               request_id="resp_known", actual_cost_usd=0.00003,
                               failure_stage="identity_validation")
    known_values = attempt_failure_accounting(known, prior)
    assert known_values["status"] == "VALIDATION_FAILED"
    assert known_values["actual_cost_usd"] == "0.00003"
    assert json.loads(known_values["usage"]) == {"input_tokens": 10, "output_tokens": 5}
    assert json.loads(known_values["provider_response"])["failure_stage"] == "identity_validation"

