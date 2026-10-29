"""Structured evidence contract and opt-in semantic analysis boundary.

This module deliberately does not infer meaning from keyword counts. The
deterministic baseline preserves source material and existing candidate claims
for review. A semantic provider may be injected only by an explicitly
authorized caller; no provider or network client is configured here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse


CONTRACT_VERSION = "structured-company-analysis-v1"
CONTRACT_RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["version", "entity_identity", "products_services", "target_audiences",
                 "capabilities_characteristics", "source_facts", "proposed_claims",
                 "claim_to_evidence", "claim_reviews", "evidence_limitations",
                 "observed_relationships", "potential_geo_implications",
                 "unsupported_or_unresolved_claims", "final_personalization_claim_ids",
                 "recommendation_decision"],
    "properties": {
        "version": {"const": CONTRACT_VERSION},
        "entity_identity": {"type": "object", "required": ["evidence_ids"]},
        "products_services": {"type": "array"},
        "target_audiences": {"type": "array"},
        "capabilities_characteristics": {"type": "array"},
        "source_facts": {"type": "array"},
        "proposed_claims": {"type": "array"},
        "claim_to_evidence": {"type": "object"},
        "claim_reviews": {"type": "array"},
        "evidence_limitations": {"type": "array", "items": {"type": "string"}},
        "observed_relationships": {"type": "array"},
        "potential_geo_implications": {"type": "array"},
        "unsupported_or_unresolved_claims": {"type": "array", "items": {"type": "string"}},
        "final_personalization_claim_ids": {"type": "array", "items": {"type": "string"}},
        "recommendation_decision": {"type": "string"},
    },
    "additionalProperties": False,
}


class SupportStatus(str, Enum):
    FULLY_SUPPORTED = "FULLY_SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    NOT_VERIFIABLE_FROM_SAMPLE = "NOT_VERIFIABLE_FROM_SAMPLE"


@dataclass(frozen=True)
class SourceFact:
    id: str
    # Normalized or summarized text used for analysis. This is never treated
    # as the original website wording.
    text: str
    exact_excerpt: str
    source_url: str
    # Immutable source capture used to verify exact_excerpt. For compact
    # evidence records this may be the source paragraph, not an entire page.
    source_content: str = ""
    source_title: str = ""
    section: str = ""
    attribution: str = "FIRST_PARTY_PUBLISHED"
    observed_at: str = ""


@dataclass(frozen=True)
class ProposedClaim:
    id: str
    text: str
    evidence_ids: tuple[str, ...]
    attribution: str
    role: str


@dataclass(frozen=True)
class ClaimReview:
    claim_id: str
    status: SupportStatus
    rationale: str
    evidence_ids: tuple[str, ...] = ()
    reviewer: str = "AUTOMATED_STRUCTURAL_ONLY"


@dataclass(frozen=True)
class SiteRelationship:
    id: str
    description: str
    evidence_ids: tuple[str, ...]
    kind: str = "OBSERVED_RELATIONSHIP"


@dataclass(frozen=True)
class GeoHypothesis:
    id: str
    hypothesis: str
    evidence_ids: tuple[str, ...]
    relationship_ids: tuple[str, ...] = ()
    status: str = "HYPOTHESIS_REQUIRES_REVIEW"


@dataclass(frozen=True)
class EvidenceContract:
    version: str
    entity_identity: Mapping[str, Any]
    products_services: tuple[Mapping[str, Any], ...]
    target_audiences: tuple[Mapping[str, Any], ...]
    capabilities_characteristics: tuple[Mapping[str, Any], ...]
    source_facts: tuple[SourceFact, ...]
    proposed_claims: tuple[ProposedClaim, ...]
    claim_to_evidence: Mapping[str, tuple[str, ...]]
    claim_reviews: tuple[ClaimReview, ...]
    evidence_limitations: tuple[str, ...]
    observed_relationships: tuple[SiteRelationship, ...]
    potential_geo_implications: tuple[GeoHypothesis, ...]
    unsupported_or_unresolved_claims: tuple[str, ...]
    final_personalization_claim_ids: tuple[str, ...] = ()
    recommendation_decision: str = "ABSTAIN_PENDING_SEMANTIC_REVIEW"

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _source_normalize(value: str) -> str:
    """Collapse whitespace for provenance matching without rewriting facts."""
    return " ".join(str(value or "").split())


def validate_contract(contract: EvidenceContract) -> list[str]:
    """Validate shape and provenance only; this is not semantic verification."""
    errors: list[str] = []
    if contract.version != CONTRACT_VERSION:
        errors.append("UNSUPPORTED_CONTRACT_VERSION")
    facts = {fact.id: fact for fact in contract.source_facts}
    if len(facts) != len(contract.source_facts):
        errors.append("DUPLICATE_FACT_ID")
    for fact in contract.source_facts:
        if not fact.id or not fact.text or not fact.exact_excerpt:
            errors.append("INCOMPLETE_SOURCE_FACT")
        if not _valid_http_url(fact.source_url):
            errors.append("INVALID_SOURCE_URL")
        if not fact.source_content:
            errors.append("SOURCE_CONTENT_MISSING")
        elif (_source_normalize(fact.exact_excerpt) and
              _source_normalize(fact.exact_excerpt) not in _source_normalize(fact.source_content)):
            errors.append("EXCERPT_NOT_IN_SOURCE_CONTENT")
    for field_name, entries in (
        ("entity_identity", (contract.entity_identity,)),
        ("products_services", contract.products_services),
        ("target_audiences", contract.target_audiences),
        ("capabilities_characteristics", contract.capabilities_characteristics),
    ):
        for entry in entries:
            refs = entry.get("evidence_ids", ())
            if not refs:
                errors.append(f"{field_name.upper()}_MISSING_EVIDENCE")
            if any(ref not in facts for ref in refs):
                errors.append(f"{field_name.upper()}_UNKNOWN_EVIDENCE")
    claim_ids = {claim.id for claim in contract.proposed_claims}
    if len(claim_ids) != len(contract.proposed_claims):
        errors.append("DUPLICATE_CLAIM_ID")
    for claim in contract.proposed_claims:
        refs = contract.claim_to_evidence.get(claim.id, ())
        if not claim.evidence_ids or tuple(refs) != tuple(claim.evidence_ids):
            errors.append("CLAIM_EVIDENCE_MAPPING_MISMATCH")
        if any(ref not in facts for ref in claim.evidence_ids):
            errors.append("CLAIM_REFERENCES_UNKNOWN_FACT")
    for review in contract.claim_reviews:
        if review.claim_id not in claim_ids:
            errors.append("REVIEW_REFERENCES_UNKNOWN_CLAIM")
        if any(ref not in facts for ref in review.evidence_ids):
            errors.append("REVIEW_REFERENCES_UNKNOWN_FACT")
    for relationship in contract.observed_relationships:
        if any(ref not in facts for ref in relationship.evidence_ids):
            errors.append("RELATIONSHIP_REFERENCES_UNKNOWN_FACT")
    for hypothesis in contract.potential_geo_implications:
        if any(ref not in facts for ref in hypothesis.evidence_ids):
            errors.append("HYPOTHESIS_REFERENCES_UNKNOWN_FACT")
        if any(ref not in {x.id for x in contract.observed_relationships}
               for ref in hypothesis.relationship_ids):
            errors.append("HYPOTHESIS_REFERENCES_UNKNOWN_RELATIONSHIP")
    for claim_id in contract.final_personalization_claim_ids:
        if claim_id not in claim_ids:
            errors.append("FINAL_COPY_REFERENCES_UNKNOWN_CLAIM")
        reviews = [x for x in contract.claim_reviews if x.claim_id == claim_id]
        if not reviews or all(x.status != SupportStatus.FULLY_SUPPORTED for x in reviews):
            errors.append("FINAL_COPY_CLAIM_NOT_FULLY_SUPPORTED")
    if contract.recommendation_decision == "RECOMMEND":
        if not contract.potential_geo_implications:
            errors.append("RECOMMENDATION_WITHOUT_HYPOTHESIS")
        if any(x.status != "HYPOTHESIS_REQUIRES_REVIEW" for x in contract.potential_geo_implications):
            errors.append("GEO_IMPLICATION_NOT_MARKED_HYPOTHESIS")
    return list(dict.fromkeys(errors))


def contract_from_dict(raw: Mapping[str, Any]) -> EvidenceContract:
    """Parse a provider/human JSON object into the typed evidence contract."""
    facts = tuple(SourceFact(**item) for item in raw.get("source_facts", ()))
    claims = tuple(ProposedClaim(
        id=item["id"], text=item["text"], evidence_ids=tuple(item.get("evidence_ids", ())),
        attribution=item["attribution"], role=item["role"])
        for item in raw.get("proposed_claims", ()))
    reviews = tuple(ClaimReview(
        claim_id=item["claim_id"], status=SupportStatus(item["status"]),
        rationale=item["rationale"], evidence_ids=tuple(item.get("evidence_ids", ())),
        reviewer=item.get("reviewer", "MODEL_CANDIDATE"))
        for item in raw.get("claim_reviews", ()))
    relationships = tuple(SiteRelationship(
        id=item["id"], description=item["description"],
        evidence_ids=tuple(item.get("evidence_ids", ())), kind=item.get("kind", "OBSERVED_RELATIONSHIP"))
        for item in raw.get("observed_relationships", ()))
    hypotheses = tuple(GeoHypothesis(
        id=item["id"], hypothesis=item["hypothesis"],
        evidence_ids=tuple(item.get("evidence_ids", ())),
        relationship_ids=tuple(item.get("relationship_ids", ())),
        status=item.get("status", "HYPOTHESIS_REQUIRES_REVIEW"))
        for item in raw.get("potential_geo_implications", ()))
    contract = EvidenceContract(
        version=raw["version"], entity_identity=raw["entity_identity"],
        products_services=tuple(raw["products_services"]),
        target_audiences=tuple(raw["target_audiences"]),
        capabilities_characteristics=tuple(raw["capabilities_characteristics"]),
        source_facts=facts, proposed_claims=claims,
        claim_to_evidence={k: tuple(v) for k, v in raw["claim_to_evidence"].items()},
        claim_reviews=reviews,
        evidence_limitations=tuple(raw["evidence_limitations"]),
        observed_relationships=relationships, potential_geo_implications=hypotheses,
        unsupported_or_unresolved_claims=tuple(raw["unsupported_or_unresolved_claims"]),
        final_personalization_claim_ids=tuple(raw["final_personalization_claim_ids"]),
        recommendation_decision=raw["recommendation_decision"],
    )
    errors = validate_contract(contract)
    if errors:
        raise ValueError("Evidence contract failed structural validation: " + ",".join(errors))
    return contract


class SemanticBackend(Protocol):
    def analyze_json(self, *, system_instruction: str, evidence: Mapping[str, Any],
                     schema: Mapping[str, Any], model: str) -> Mapping[str, Any]: ...


class SemanticAnalysisNotAuthorized(RuntimeError):
    pass


class ConstrainedSemanticAnalyzer:
    """Adapter for a caller-supplied model backend; makes no implicit API calls."""

    SYSTEM_INSTRUCTION = (
        "Use only the supplied first-party evidence. Return structured JSON. "
        "Every factual claim must cite source fact IDs. Separate company-stated "
        "claims from observed structure and hypotheses. Do not infer site-wide "
        "absence from a bounded sample. Mark uncertainty and abstain when support "
        "is incomplete. Do not invent rankings, performance, audiences, or outcomes."
    )

    def __init__(self, backend: SemanticBackend, *, model: str, authorized: bool = False):
        self.backend = backend
        self.model = model
        self.authorized = authorized

    def analyze(self, evidence: Mapping[str, Any], schema: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.authorized:
            raise SemanticAnalysisNotAuthorized(
                "Model analysis requires an explicitly authorized provider, model, and budget."
            )
        return self.backend.analyze_json(system_instruction=self.SYSTEM_INSTRUCTION,
                                          evidence=evidence, schema=schema, model=self.model)

    def analyze_contract(self, evidence: Mapping[str, Any]) -> EvidenceContract:
        raw = self.analyze(evidence, CONTRACT_RESPONSE_SCHEMA)
        return contract_from_dict(raw)


class DeterministicBaseline:
    """Wrap existing deterministic output without claiming semantic validation."""

    def analyze(self, *, company_name: str, website: str,
                personalization: Mapping[str, Any]) -> EvidenceContract:
        profile = personalization.get("evidence_profile") or {}
        pages = profile.get("pages") or []
        raw_facts = profile.get("facts") or []
        facts = tuple(SourceFact(
            id=str(item.get("id", "")),
            text=str(item.get("evidence_excerpt") or item.get("statement") or ""),
            exact_excerpt=str(item.get("evidence_excerpt") or ""),
            source_url=str((item.get("source_urls") or [""])[0]),
            source_content=str(item.get("source_content") or ""),
            source_title=str(item.get("source_title") or ""),
            section=str(item.get("section_heading") or ""),
            attribution=str(item.get("attribution") or "FIRST_PARTY_PUBLISHED"),
            observed_at=str(personalization.get("retrieved_at") or ""),
        ) for item in raw_facts)
        by_id = {fact.id: fact for fact in facts}
        claims: list[ProposedClaim] = []
        reviews: list[ClaimReview] = []
        mappings: dict[str, tuple[str, ...]] = {}
        for role, key in (("BUSINESS_DESCRIPTION", "business_description"),
                          ("COMPANY_STRENGTH", "company_strength")):
            text = str(personalization.get(key) or "").strip()
            ids = tuple(str(x) for x in (personalization.get("claim_support") or {}).get(key, ())
                        if str(x) in by_id)
            if not text:
                continue
            claim_id = role.lower()
            claim = ProposedClaim(claim_id, text, ids, "COMPANY_STATED", role)
            claims.append(claim)
            mappings[claim_id] = ids
            reviews.append(ClaimReview(
                claim_id, SupportStatus.NOT_VERIFIABLE_FROM_SAMPLE,
                "The deterministic implementation records provenance and lexical overlap, "
                "but does not evaluate semantic entailment or distinctiveness.", ids
            ))
        relationships = tuple(SiteRelationship(
            id=f"relationship_{index}",
            description=str(item.get("interpretation") or item.get("kind") or "Observed source relationship."),
            evidence_ids=tuple(fact.id for fact in facts
                               if any(url in fact.source_url for url in item.get("source_urls", ()))),
        ) for index, item in enumerate(profile.get("relationships") or []))
        limitations = list((personalization.get("assessment") or {}).get("limitations") or [])
        limitations.append("Deterministic baseline does not perform semantic entailment or human quality judgment.")
        identity_refs = tuple(fact.id for fact in facts
                              if company_name.casefold() in fact.exact_excerpt.casefold())[:3]
        return EvidenceContract(
            version=CONTRACT_VERSION,
            entity_identity={"name": company_name, "website": website,
                             "identity_status": "INPUT_CANDIDATE_NOT_INDEPENDENTLY_VERIFIED",
                             "evidence_ids": identity_refs},
            products_services=(), target_audiences=(), capabilities_characteristics=(),
            source_facts=facts, proposed_claims=tuple(claims), claim_to_evidence=mappings,
            claim_reviews=tuple(reviews), evidence_limitations=tuple(dict.fromkeys(limitations)),
            observed_relationships=relationships, potential_geo_implications=(),
            unsupported_or_unresolved_claims=tuple(claim.id for claim in claims),
            recommendation_decision="ABSTAIN_NO_SEMANTIC_REVIEW",
        )


def make_contract(*, entity_identity: Mapping[str, Any],
                  products_services: Sequence[Mapping[str, Any]] = (),
                  target_audiences: Sequence[Mapping[str, Any]] = (),
                  capabilities_characteristics: Sequence[Mapping[str, Any]] = (),
                  source_facts: Sequence[SourceFact] = (),
                  proposed_claims: Sequence[ProposedClaim] = (),
                  claim_reviews: Sequence[ClaimReview] = (),
                  evidence_limitations: Sequence[str] = (),
                  observed_relationships: Sequence[SiteRelationship] = (),
                  potential_geo_implications: Sequence[GeoHypothesis] = (),
                  unsupported_or_unresolved_claims: Sequence[str] = (),
                  final_personalization_claim_ids: Sequence[str] = (),
                  recommendation_decision: str = "ABSTAIN") -> EvidenceContract:
    claims = tuple(proposed_claims)
    return EvidenceContract(
        version=CONTRACT_VERSION, entity_identity=dict(entity_identity),
        products_services=tuple(products_services), target_audiences=tuple(target_audiences),
        capabilities_characteristics=tuple(capabilities_characteristics),
        source_facts=tuple(source_facts), proposed_claims=claims,
        claim_to_evidence={claim.id: claim.evidence_ids for claim in claims},
        claim_reviews=tuple(claim_reviews), evidence_limitations=tuple(evidence_limitations),
        observed_relationships=tuple(observed_relationships),
        potential_geo_implications=tuple(potential_geo_implications),
        unsupported_or_unresolved_claims=tuple(unsupported_or_unresolved_claims),
        final_personalization_claim_ids=tuple(final_personalization_claim_ids),
        recommendation_decision=recommendation_decision,
    )
