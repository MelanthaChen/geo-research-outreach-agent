from __future__ import annotations

import pytest

from outreach_v2.semantic_analysis import (
    CONTRACT_VERSION,
    ClaimReview,
    ConstrainedSemanticAnalyzer,
    DeterministicBaseline,
    EvidenceContract,
    ProposedClaim,
    SemanticAnalysisNotAuthorized,
    SourceFact,
    SupportStatus,
    contract_from_dict,
    make_contract,
    validate_contract,
)


def fact():
    return SourceFact("f1", "The clinic offers pediatric dentistry.",
                      "The clinic offers pediatric dentistry.",
                      "https://clinic.example/services",
                      source_content="The clinic offers pediatric dentistry.",
                      attribution="COMPANY_STATED")


def test_source_excerpt_is_matched_to_original_source_not_normalized_statement():
    source = SourceFact(
        "f1", "Clinic offers pediatric dentistry.",
        "The clinic offers pediatric dentistry, including preventive care.",
        "https://clinic.example/services",
        source_content="Services   The clinic offers pediatric dentistry,\nincluding preventive care.",
        attribution="COMPANY_STATED")
    contract = make_contract(entity_identity={"name": "Clinic", "evidence_ids": ["f1"]},
                             source_facts=(source,))
    assert "EXCERPT_NOT_IN_SOURCE_CONTENT" not in validate_contract(contract)
    assert "EXCERPT_NOT_IN_SOURCE_FACT" not in validate_contract(contract)


def test_source_excerpt_must_match_captured_source_content():
    source = SourceFact(
        "f1", "Clinic offers pediatric dentistry.",
        "Clinic offers orthodontic services.",
        "https://clinic.example/services",
        source_content="The clinic offers pediatric dentistry.",
        attribution="COMPANY_STATED")
    contract = make_contract(entity_identity={"name": "Clinic", "evidence_ids": ["f1"]},
                             source_facts=(source,))
    assert "EXCERPT_NOT_IN_SOURCE_CONTENT" in validate_contract(contract)


def test_source_excerpt_without_original_source_capture_is_not_verified():
    source = SourceFact("f1", "Clinic offers pediatric dentistry.",
                       "Clinic offers pediatric dentistry.",
                       "https://clinic.example/services", attribution="COMPANY_STATED")
    contract = make_contract(entity_identity={"name": "Clinic", "evidence_ids": ["f1"]},
                             source_facts=(source,))
    assert "SOURCE_CONTENT_MISSING" in validate_contract(contract)


def test_contract_requires_provenance_for_each_identity_and_claim():
    claim = ProposedClaim("c1", "The clinic offers pediatric dentistry.", ("f1",),
                          "COMPANY_STATED", "DESCRIPTION")
    contract = make_contract(
        entity_identity={"name": "Clinic", "website": "https://clinic.example",
                         "evidence_ids": ["f1"]},
        products_services=({"value": "pediatric dentistry", "evidence_ids": ["f1"]},),
        source_facts=(fact(),), proposed_claims=(claim,),
        claim_reviews=(ClaimReview("c1", SupportStatus.FULLY_SUPPORTED,
                                   "Direct first-party statement.", ("f1",), "HUMAN"),),
        final_personalization_claim_ids=("c1",), recommendation_decision="ABSTAIN")
    assert validate_contract(contract) == []
    assert contract.to_dict()["version"] == CONTRACT_VERSION
    assert contract_from_dict(contract.to_dict()) == contract


def test_final_copy_rejects_unreviewed_claim_even_when_url_and_fact_exist():
    claim = ProposedClaim("c1", "The clinic offers pediatric dentistry.", ("f1",),
                          "COMPANY_STATED", "DESCRIPTION")
    contract = make_contract(
        entity_identity={"name": "Clinic", "website": "https://clinic.example",
                         "evidence_ids": ["f1"]},
        source_facts=(fact(),), proposed_claims=(claim,),
        claim_reviews=(ClaimReview("c1", SupportStatus.NOT_VERIFIABLE_FROM_SAMPLE,
                                   "Lexical overlap is not semantic review.", ("f1",)),),
        final_personalization_claim_ids=("c1",))
    assert "FINAL_COPY_CLAIM_NOT_FULLY_SUPPORTED" in validate_contract(contract)


def test_structural_validator_detects_missing_mapping_and_invalid_source():
    invalid_fact = SourceFact("f1", "A statement.", "A statement.", "file:///private/page")
    claim = ProposedClaim("c1", "A statement.", ("unknown",), "COMPANY_STATED", "DESCRIPTION")
    contract = EvidenceContract(
        version=CONTRACT_VERSION, entity_identity={"name": "X", "evidence_ids": ["missing"]},
        products_services=(), target_audiences=(), capabilities_characteristics=(),
        source_facts=(invalid_fact,), proposed_claims=(claim,), claim_to_evidence={},
        claim_reviews=(), evidence_limitations=(), observed_relationships=(),
        potential_geo_implications=(), unsupported_or_unresolved_claims=(),
    )
    errors = validate_contract(contract)
    assert "INVALID_SOURCE_URL" in errors
    assert "CLAIM_EVIDENCE_MAPPING_MISMATCH" in errors
    assert "CLAIM_REFERENCES_UNKNOWN_FACT" in errors
    assert "ENTITY_IDENTITY_UNKNOWN_EVIDENCE" in errors


def test_geo_recommendation_requires_an_explicit_hypothesis():
    contract = make_contract(entity_identity={"name": "Clinic", "evidence_ids": ["f1"]},
                             source_facts=(fact(),), recommendation_decision="RECOMMEND")
    assert "RECOMMENDATION_WITHOUT_HYPOTHESIS" in validate_contract(contract)


def test_model_adapter_makes_no_call_without_explicit_authorization():
    class Backend:
        called = False
        def analyze_json(self, **kwargs):
            self.called = True
            return {}

    backend = Backend()
    analyzer = ConstrainedSemanticAnalyzer(backend, model="unused")
    with pytest.raises(SemanticAnalysisNotAuthorized):
        analyzer.analyze({}, {})
    assert not backend.called


@pytest.mark.parametrize("status", list(SupportStatus))
def test_claim_review_contract_preserves_each_semantic_judgment_status(status):
    review = ClaimReview("c1", status, "Recorded reviewer rationale.", ("f1",), "HUMAN")
    assert review.status.value == status.value
    assert review.reviewer == "HUMAN"


def test_authorized_model_adapter_is_injectable_and_receives_grounding_contract():
    class FakeBackend:
        def __init__(self):
            self.request = None
        def analyze_json(self, **kwargs):
            self.request = kwargs
            return {"proposed_claims": [], "recommendation_decision": "ABSTAIN"}

    backend = FakeBackend()
    analyzer = ConstrainedSemanticAnalyzer(backend, model="test-only", authorized=True)
    schema = {"type": "object", "required": ["proposed_claims"]}
    result = analyzer.analyze({"source_facts": [{"id": "f1"}]}, schema)
    assert result == {"proposed_claims": [], "recommendation_decision": "ABSTAIN"}
    assert backend.request["schema"] == schema
    assert "only the supplied first-party evidence" in backend.request["system_instruction"].lower()
    assert backend.request["model"] == "test-only"


def test_deterministic_baseline_preserves_old_claim_but_refuses_to_call_it_semantically_verified():
    profile = {
        "business_description": "Lagoon is a concept store, gallery, and creation space.",
        "company_strength": "Its custom design service lets clients tailor a garment.",
        "claim_support": {"business_description": ["f1"], "company_strength": ["f1"]},
        "evidence_profile": {"pages": [{"url": "https://lagoon.example/about"}],
            "facts": [{"id": "f1", "statement": "Lagoon is a concept store, gallery, and creation space.",
                       "evidence_excerpt": "Lagoon is a concept store, gallery, and creation space.",
                       "source_urls": ["https://lagoon.example/about"],
                       "attribution": "COMPANY_STATED"}]},
        "assessment": {"limitations": []}, "retrieved_at": "2026-10-28T12:00:00Z",
    }
    result = DeterministicBaseline().analyze(company_name="Lagoon", website="https://lagoon.example/",
                                             personalization=profile)
    assert result.claim_reviews[0].status is SupportStatus.NOT_VERIFIABLE_FROM_SAMPLE
    assert result.recommendation_decision == "ABSTAIN_NO_SEMANTIC_REVIEW"
    assert result.unsupported_or_unresolved_claims == ("business_description", "company_strength")
