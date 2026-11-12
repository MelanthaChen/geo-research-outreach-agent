"""Single-request OpenAI semantic analysis with strict local preflight.

The adapter deliberately uses one direct HTTPS POST through httpx (no SDK retry
layer), and requires a local tokenizer and a fixed, published price schedule
before it will send anything. Model-produced support judgments are never
treated as independent verification.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from .semantic_analysis import (
    CONTRACT_VERSION,
    ClaimReview,
    EvidenceContract,
    GeoHypothesis,
    SiteRelationship,
    SourceFact,
    SupportStatus,
    ProposedClaim,
    validate_contract,
)

MODEL = "gpt-5.4-mini-2026-03-17"
API_URL = "https://api.openai.com/v1/responses"
PRICE_INPUT_PER_MILLION = 0.75
PRICE_OUTPUT_PER_MILLION = 4.50
MAX_INPUT_TOKENS = 15_000
MAX_OUTPUT_TOKENS = 6_000  # Responses API counts reasoning tokens in this cap.
MAX_ESTIMATED_USD = 0.05
PRICING_SOURCE = "https://developers.openai.com/api/docs/models/gpt-5.4-mini"


def _obj(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties),
            "required": list(properties), "additionalProperties": False}


_refs = {"type": "array", "items": {"type": "string"}}
_text = {"type": "string"}
_entity_item = _obj({"summary": _text, "evidence_ids": _refs, "attribution": {
    "type": "string", "enum": ["COMPANY_STATED", "PUBLISHED_STRUCTURED_DATA", "ANALYTICAL_INTERPRETATION"]}})
_claim = _obj({"id": {"type": "string"}, "text": _text, "evidence_ids": _refs,
               "attribution": {"type": "string", "enum": ["COMPANY_STATED", "ANALYTICAL_INTERPRETATION", "GEO_HYPOTHESIS"]},
               "role": {"type": "string", "enum": ["BUSINESS_DESCRIPTION", "COMPANY_CHARACTERISTIC", "RESEARCH_RELEVANCE", "GEO_OBSERVATION"]}})
_relationship = _obj({"id": {"type": "string"}, "description": _text, "evidence_ids": _refs,
                      "kind": {"type": "string"}})
_hypothesis = _obj({"id": {"type": "string"}, "observation": _text, "hypothesis": _text,
                    "recommendation": _text, "evidence_ids": _refs,
                    "relationship_ids": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": ["HYPOTHESIS_REQUIRES_REVIEW"]}})

OPENAI_ANALYSIS_SCHEMA: Mapping[str, Any] = _obj({
    "version": {"type": "string", "enum": [CONTRACT_VERSION]},
    "entity_identity": _obj({"name": _text, "website": _text, "identity_status": _text,
                              "evidence_ids": _refs}),
    "products_services": {"type": "array", "items": _entity_item},
    "target_audiences": {"type": "array", "items": _entity_item},
    "capabilities_characteristics": {"type": "array", "items": _entity_item},
    "proposed_claims": {"type": "array", "items": _claim},
    "evidence_limitations": {"type": "array", "items": _text},
    "observed_relationships": {"type": "array", "items": _relationship},
    "potential_geo_implications": {"type": "array", "items": _hypothesis},
    "unsupported_or_unresolved_claims": {"type": "array", "items": _text},
    "recommendation_decision": {"type": "string", "enum": ["RECOMMEND", "ABSTAIN"]},
})

SYSTEM_INSTRUCTION = """Analyze only the supplied first-party website evidence. Return the required JSON object, not prose outside JSON. The evidence classifications and attributions are supplied by deterministic application code; do not promote EVENT_OR_PARTICIPANT, THIRD_PARTY_OR_PARTNER, CUSTOMER_OR_TESTIMONIAL, or AMBIGUOUS material into a claim about the target business. For every factual claim, cite one or more exact evidence IDs. Summarize in concise natural language; do not copy long source passages. Distinguish company-published statements from your interpretations. Describe the business, offerings, customer-facing characteristics, and why this site is relevant to research about AI systems representing businesses. Analyze page relationships only from the supplied page structure and evidence. Any GEO implication is a hypothesis, never a measured result. Do not claim search rankings, AI visibility, performance, or site-wide absences. Return no recommendation unless a concrete opportunity is directly supported by the evidence; otherwise use ABSTAIN and an empty hypotheses array. State evidence limitations and unresolved questions. Do not claim your own support judgment is independent verification."""

_UNSUPPORTED_COMPARATIVE_RE = re.compile(
    r"\b(?:best|largest|smallest|leading|only|unique|first(?![-‐‑‒–— ]party\b)|most)\b", re.I)


def _has_unsupported_comparative(text: str) -> bool:
    return bool(_UNSUPPORTED_COMPARATIVE_RE.search(text))


_OFFERING_TERMS = re.compile(
    r"\b(?:accommodations?|amenities|breakfast|brunch|classes|cleaning|coffee|coffees|"
    r"cafe|cafes|cocktails?|cuisine|delivery|dining|dishes|drinks|events?|fabrication|"
    r"films?|food|gatherings?|glass|laundry|lunch|lodging|meals?|menu|mirrors?|"
    r"nightlife|office space|packages?|performances?|products?|rentals?|resilvering|"
    r"rooms?|rugs?|services?|shows?|specials|sports bars?|suites?|tacos?|tastings?|"
    r"theater|weddings?|wine|workspace|workspaces)\b", re.I)
_OFFERING_ACTIONS = re.compile(
    r"\b(?:offer(?:s|ing)?|provide(?:s|d)?|serve(?:s|d|ing)?|sell(?:s|ing)?|"
    r"include(?:s|d)?|feature(?:s|d)?|brew(?:s|ing)?|roast(?:s|ing)?|"
    r"prepare(?:s|d)?|cook(?:s|ing)?|host(?:s|ed|ing)?|rent(?:s|ed|ing)?|"
    r"repair(?:s|ed|ing)?|clean(?:s|ed|ing)?|launder(?:s|ed|ing)?|"
    r"fabricate(?:s|d|ing)?|resilver(?:s|ed|ing)?|deliver(?:s|ed|ing)?|"
    r"order(?:s|ed|ing)?|available|open|feature(?:s|d)?|have)\b", re.I)


def _direct_offering_fact(fact: Mapping[str, Any], company_name: str) -> bool:
    """Recognize explicit first-party offerings without changing provenance labels.

    Some sites describe what they sell or host in company-description copy,
    menus, hotel room descriptions, and catalog cards rather than on a URL
    named /services. The existing TARGET_BUSINESS classification still
    protects identity checks; this narrower, separate predicate is used only
    to choose a cited offering for recipient personalization.
    """
    if fact.get("classification") == "PRODUCT_OR_SERVICE":
        return True
    if fact.get("classification") != "TARGET_BUSINESS":
        return False
    text = " ".join(str(fact.get("exact_excerpt") or fact.get("text") or "").split())
    if not text or not _OFFERING_TERMS.search(text):
        return False
    # The extractor has already excluded customer quotes, event participants,
    # third parties, cross-site URLs, and ambiguous material. Require direct
    # company voice, a named-business anchor, or a category-specific service
    # description before treating the passage as an offering.
    company_tokens = [token for token in re.findall(r"[A-Za-z0-9]+", company_name)
                      if len(token) >= 4 and token.casefold() not in {"company", "incorporated"}]
    named = any(re.search(rf"\b{re.escape(token)}\b", text, re.I) for token in company_tokens)
    company_voice = bool(re.search(r"\b(?:we|our|us)\b", text, re.I))
    if not (named or company_voice):
        return False
    if _OFFERING_ACTIONS.search(text):
        return True
    # Compact catalog and hospitality copy often omits an explicit verb:
    # “COLLECTION ... Persian rugs” and “guest rooms ... accommodations”.
    if re.search(
        r"\b(?:collection|catalog(?:ue)?|menu|guest rooms?|rooms?|suites?|"
        r"accommodations?|membership|office space|meeting rooms?)\b.{0,160}"
        r"\b(?:rugs?|food|dishes|meals?|coffee|coffees|beer|wine|"
        r"amenities|lodging|stays?|events?|classes|performances?|"
        r"products?|services?|office space|meeting rooms?)\b", text, re.I):
        return True
    # Verified first-party pages often use concise catalog or venue copy without
    # an offer verb (“our guest rooms,” “coffee delivered,” “a sports bar”).
    # Require an explicit product, service, room, food, drink, or event term
    # plus a named-business or first-person attribution anchor.
    return bool(_OFFERING_TERMS.search(text))


class PreflightError(RuntimeError):
    """A fail-closed validation error before any network request."""


class CredentialUnavailable(PreflightError):
    pass


@dataclass(frozen=True)
class EvidenceSelection:
    target_company: str
    website: str
    pages: tuple[Mapping[str, Any], ...]
    facts: tuple[Mapping[str, Any], ...]
    classifications: Mapping[str, str]
    excluded_reasons: Mapping[str, str]
    excluded_counts: Mapping[str, int]
    limitations: tuple[str, ...]
    legacy_classifications: Mapping[str, str] = field(default_factory=dict)

    def to_input(self) -> dict[str, Any]:
        # source_content is kept by the application for excerpt provenance
        # checks. The model receives only the selected statement and exact
        # excerpt, avoiding repeated page text in the prompt.
        selected_ids = {str(item.get("id")) for item in self.facts}
        return {"target_company": self.target_company, "website": self.website,
                "pages": [dict(x) for x in self.pages],
                "facts": [{k: v for k, v in x.items() if k != "source_content"}
                          for x in self.facts],
                # Do not expose identifiers for excluded facts: the model must
                # not be prompted with IDs it cannot cite as supplied evidence.
                "evidence_classifications": {key: value for key, value in self.classifications.items()
                                              if str(key) in selected_ids},
                "excluded_reasons": dict(self.excluded_reasons),
                "excluded_counts": dict(self.excluded_counts),
                "limitations": list(self.limitations)}

    def prior_classification_input(self) -> dict[str, Any]:
        """Rebuild the pre-fix prompt only for deterministic offering reclassification.

        This permits offline replay only when its prompt hash exactly matches
        the archived paid request. It is not a general-purpose evidence change
        or a way to reuse a response against newer website content.
        """
        evidence = self.to_input()
        if not self.legacy_classifications:
            return evidence
        for item in evidence["facts"]:
            old = self.legacy_classifications.get(str(item.get("id")))
            if old:
                item["classification"] = old
                evidence["evidence_classifications"][str(item["id"])] = old
        return evidence


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _classify_fact(item: Mapping[str, Any], site_host: str, company_name: str, *,
                   recognize_explicit_offerings: bool = True) -> str:
    urls = item.get("source_urls") or []
    if not urls or any(_host(str(url)) != site_host for url in urls):
        return "THIRD_PARTY_OR_PARTNER"
    raw = " ".join(str(item.get(k) or "") for k in
                   ("attribution", "assertion_type", "relationship", "section_heading")).upper()
    excerpt = str(item.get("evidence_excerpt") or item.get("statement") or "")
    if any(word in raw for word in ("TESTIMONIAL", "CUSTOMER", "PARTNER", "THIRD_PARTY", "PARTICIPANT")):
        return "CUSTOMER_OR_TESTIMONIAL" if "TESTIMONIAL" in raw or "CUSTOMER" in raw else "THIRD_PARTY_OR_PARTNER"
    # Service and product pages often embed customer reviews. Classify these
    # before the URL-path rule below, or a quote about a patient's experience
    # could be sent to the model as if it were the business's own service claim.
    quoted_copy = excerpt.lstrip().startswith(("\u201c", "\u2018", '"'))
    quoted_review = quoted_copy and (
        re.search(r"\b(?:I|my|we|our)\b", excerpt, re.I) is not None
        or re.search(r"\b(?:great|excellent|friendly|comfortable|recommend|happy|wonderful|professional)\b", excerpt, re.I) is not None
    )
    if quoted_review or re.search(r"\b(?:customer testimonials?|patient reviews?|guest reviews?|what customers say|what patients say)\b", raw, re.I):
        return "CUSTOMER_OR_TESTIMONIAL"
    if quoted_copy:
        # A standalone quotation with no reliable speaker metadata is
        # ambiguous. Exclude it instead of attributing it to the company.
        return "AMBIGUOUS"
    if (re.search(r"\b(?:podcast|episode|interview|show)\b", excerpt, re.I)
            and re.search(r"\b(?:sits down with|joins?|appears?|featured|interviewed|guest)\b", excerpt, re.I)):
        return "EVENT_OR_PARTICIPANT"
    path = urlparse(str(urls[0])).path.casefold()
    if "event" in path:
        # Event pages mix the host organization's programming with guest and
        # partner material. Keep it explicit as context; never use it to prove
        # a company capability or business identity.
        return "EVENT_OR_PARTICIPANT"
    if any(word in raw for word in ("AMBIGUOUS", "UNKNOWN")):
        return "AMBIGUOUS"
    typ = str(item.get("assertion_type") or "").upper()
    if typ == "STRUCTURED_FIRST_PARTY_DATA":
        field = str(item.get("field") or "").casefold()
        if field in {"name", "description", "address", "openinghours", "email", "telephone"}:
            return "TARGET_BUSINESS"
        # Offerings and hotel amenities often appear as JSON-LD properties on
        # the business object, without a dedicated /services URL.
        if field in {"offers", "makesoffer", "hasoffercatalog", "offereditem",
                     "itemoffered", "service", "servicetype", "product",
                     "productcategory", "amenityfeature", "numberofrooms",
                     "roomtype", "hasamenity"}:
            return "PRODUCT_OR_SERVICE"
        return "AMBIGUOUS"
    if any(word in path for word in ("service", "product", "collection")):
        return "PRODUCT_OR_SERVICE"
    if recognize_explicit_offerings and any(word in path for word in ("menu", "menus")):
        # A first-party menu is direct evidence of the products/services listed
        # there. Keep this gated so prior_classification_input() reconstructs
        # the exact pre-repair label for hash-checked offline replay.
        return "PRODUCT_OR_SERVICE"
    # About pages may be the canonical source for an offering (as in the
    # Lagoon New York example), even when the URL is not called /services.
    # Require explicit sales or service language before assigning this class.
    if re.search(r"\b(?:available for sale|for sale|we offer|offers?\b.{0,100}\b(?:services?|products?|custom design|classes?|treatments?|lodging|rooms?|meals?|consulting|lessons?|programs?))\b",
                 excerpt, re.I):
        return "PRODUCT_OR_SERVICE"
    # About pages often describe actual offerings without saying "services"
    # or using a /services URL. Promote only company-voice statements that
    # pair an offer/serve verb with concrete offering terms; quoted customer
    # copy and third-party evidence were excluded above.
    if recognize_explicit_offerings:
        explicit_offering = re.search(
            r"\b(?:our|we|us)\b.{0,260}\b(?:offer|offers|offering|serve|serves|provide|provides|"
            r"sell|sells|feature|features|make|makes|create|creates|specialize|specializes)\b",
            excerpt, re.I)
        concrete_offering = re.search(
            r"\b(?:appetizers?|entr(?:e|é)e?s?|chops?|seafood|pasta|cocktails?|scotches?|cordials?|"
            r"beers?|wines?|menu|meals?|cuisine|food|beverages?|rooms?|lodging|stays?|amenities|"
            r"treatments?|classes?|lessons?|consulting|custom design|repairs?|installations?|"
            r"products?|services?|programs?|workshops?|garments?|fittings?)\b",
            excerpt, re.I)
        if explicit_offering and concrete_offering:
            return "PRODUCT_OR_SERVICE"
        # Explicit catalog language is also a company offer even when the
        # copy avoids an "offer/provide" verb (for example, "Shop a huge
        # inventory of ..."). Keep this constrained to concrete catalog
        # nouns so general editorial statements do not become services.
        catalog_offer = re.search(
            r"\b(?:shop|browse|buy|purchase|view)\b.{0,100}\b(?:inventory|catalog(?:ue)?|collection|"
            r"rugs?|products?|groceries|food|clothing|furniture|artwork)\b",
            excerpt, re.I)
        if catalog_offer:
            return "PRODUCT_OR_SERVICE"
        # Spanish first-person company statements commonly introduce retail
        # offers with "proveemos", "disponemos", "ofrecemos", or "vendemos".
        spanish_offer = re.search(
            r"\b(?:proveemos|disponemos|ofrecemos|vendemos|contamos con|tenemos)\b.{0,180}\b(?:productos?|frutas?|verduras?|vegetales?|alimentos?|"
            r"pescados?|quesos?|bebidas?|abarrotes|servicios?|comidas?)\b",
            excerpt, re.I)
        if spanish_offer:
            return "PRODUCT_OR_SERVICE"
    # An About/Home claim is target evidence only if it is explicitly tied to
    # the organization, its authors, or first-person company voice.
    entity = str(item.get("statement") or excerpt).casefold()
    company_tokens = [t.casefold() for t in re.findall(r"[A-Za-z0-9]+", company_name)]
    anchor = max((t for t in company_tokens if len(t) >= 5), key=len, default="")
    normalized_entity = re.sub(r"\s+", " ", entity)
    normalized_name = re.sub(r"\s+", " ", company_name.casefold())
    if ((normalized_name and normalized_name in normalized_entity) or
            (anchor and re.search(rf"\b{re.escape(anchor)}\b", normalized_entity)) or
            re.search(r"\b(?:we|our|us)\b", entity)):
        return "TARGET_BUSINESS"
    if typ in {"ATTRIBUTED_COMPANY_DESCRIPTION", "ATTRIBUTED_COMPANY_STRENGTH"}:
        return "TARGET_BUSINESS"
    return "AMBIGUOUS"


def select_evidence(personalization: Mapping[str, Any], *, company_name: str,
                    website: str, max_facts: int = 18) -> EvidenceSelection:
    """Deduplicate and attribute extracted facts before they reach a model."""
    profile = personalization.get("evidence_profile")
    if not isinstance(profile, Mapping):
        raise PreflightError("Missing extracted evidence profile")
    pages = profile.get("pages") or []
    raw_facts = profile.get("facts") or []
    site_host = _host(website)
    if not site_host or not pages or not raw_facts:
        raise PreflightError("Missing valid website pages or evidence facts")
    page_summaries = []
    for page in pages:
        url = str(page.get("url") or "")
        if _host(url) != site_host:
            raise PreflightError("Fetched page escaped the verified first-party host")
        page_summaries.append({
            "url": url, "type": str(page.get("type") or "unknown"),
            "title": str(page.get("title") or "")[:180],
            "heading_count": len(page.get("headings") or []),
            "link_count": len(page.get("links") or []),
            "truncated": bool(page.get("truncated")),
        })

    priority = {"TARGET_BUSINESS": 0, "PRODUCT_OR_SERVICE": 1,
                "EVENT_OR_PARTICIPANT": 2, "THIRD_PARTY_OR_PARTNER": 3,
                "CUSTOMER_OR_TESTIMONIAL": 4, "AMBIGUOUS": 5}
    seen: set[str] = set()
    selected: list[tuple[int, int, dict[str, Any]]] = []
    counts: dict[str, int] = {k: 0 for k in priority}
    classifications: dict[str, str] = {}
    legacy_classifications: dict[str, str] = {}
    excluded_reasons: dict[str, str] = {}
    from .quality import CODE_MARKERS
    for index, item in enumerate(raw_facts):
        if not isinstance(item, Mapping):
            continue
        excerpt = " ".join(str(item.get("evidence_excerpt") or item.get("statement") or "").split())
        urls = item.get("source_urls") or []
        fact_id = str(item.get("id") or f"candidate_{index}")
        classification = _classify_fact(item, site_host, company_name)
        previous_classification = _classify_fact(
            item, site_host, company_name, recognize_explicit_offerings=False)
        if classification != previous_classification:
            legacy_classifications[fact_id] = previous_classification
        classifications[fact_id] = classification
        counts[classification] += 1
        if not excerpt or not urls:
            excluded_reasons[fact_id] = "MISSING_EXCERPT_OR_SOURCE_URL"
            continue
        # Normalize only for duplicate detection; preserve the original quote.
        fingerprint = re.sub(r"[^a-z0-9]+", " ", excerpt.casefold()).strip()
        if not fingerprint or fingerprint in seen:
            excluded_reasons[fact_id] = "DUPLICATE_EXCERPT"
            continue
        seen.add(fingerprint)
        if CODE_MARKERS.search(excerpt):
            excluded_reasons[fact_id] = "CODE_CONTAMINATION"
            continue
        # Event/participant and uncertain material is kept out of model input;
        # page-level event structure remains available for relationship analysis.
        if classification not in {"TARGET_BUSINESS", "PRODUCT_OR_SERVICE"}:
            excluded_reasons[fact_id] = f"NON_TARGET_ATTRIBUTION:{classification}"
            continue
        assertion_type = str(item.get("assertion_type") or "")
        field = str(item.get("field") or "").casefold()
        if assertion_type.upper() == "STRUCTURED_FIRST_PARTY_DATA" and field != "description":
            excluded_reasons[fact_id] = "LOW_INFORMATION_STRUCTURED_FIELD"
            continue
        if (re.search(r"(?:\$\s?\d|\b\d+\s*/\s*(?:hr|hour)|\bfee\b|non-refundable|deposit|damaged|payment policy)", excerpt, re.I)
                or re.search(r"\b(?:please inquire|email us to inquire|contact us|leave this field empty|follow us|we also ask to receive)\b", excerpt, re.I)
                or re.search(r"\bif nothing above met your criteria\b", excerpt, re.I)):
            excluded_reasons[fact_id] = "OPERATIONAL_TERMS_OR_GENERIC_CONTACT_COPY"
            continue
        # Rank by attribution directness and source type, not lexical similarity.
        rank = (0 if assertion_type == "ATTRIBUTED_COMPANY_DESCRIPTION" else
                1 if assertion_type == "ATTRIBUTED_COMPANY_STRENGTH" else
                2 if classification == "PRODUCT_OR_SERVICE" else 3)
        fact = {
            "id": fact_id,
            "text": str(item.get("statement") or excerpt),
            "exact_excerpt": excerpt[:1000],
            "source_content": str(item.get("source_content") or ""),
            "source_url": str(urls[0]),
            "source_title": str(item.get("source_title") or "")[:180],
            "section": str(item.get("section_heading") or "")[:180],
            "attribution": str(item.get("attribution") or "FIRST_PARTY_PUBLISHED"),
            "classification": classification,
            "assertion_type": assertion_type,
            "field": field,
        }
        if not fact["id"]:
            continue
        selected.append((rank, index, fact))
    selected.sort(key=lambda row: (row[0], row[1]))
    chosen = [row[2] for row in selected[:max_facts]]
    if not chosen:
        raise PreflightError("No unambiguous first-party company or offering evidence to analyze")
    for item in chosen:
        item["classification"] = classifications[item["id"]]
    return EvidenceSelection(company_name, website, tuple(page_summaries), tuple(chosen),
                             classifications, excluded_reasons, counts,
                             tuple(str(x) for x in ((personalization.get("assessment") or {}).get("limitations") or [])),
                             {item["id"]: legacy_classifications[item["id"]]
                              for item in chosen if item["id"] in legacy_classifications})


def _tokenizer(model: str) -> Callable[[str], int]:
    try:
        import tiktoken
        encoding = tiktoken.encoding_for_model(model)
    except Exception as exc:
        raise PreflightError("Exact model tokenizer unavailable; install the semantic-analysis extra") from exc
    def count(text: str) -> int:
        return len(encoding.encode(text, disallowed_special=()))
    count.descriptor = f"{encoding.name}; tiktoken {tiktoken.__version__}"
    return count


@dataclass(frozen=True)
class Preflight:
    model: str
    input_tokens_estimate: int
    max_output_tokens: int
    worst_case_cost_usd: float
    price_input_per_million: float
    price_output_per_million: float
    pricing_source: str
    tokenizer: str
    prompt_sha256: str


def build_preflight(evidence: Mapping[str, Any], *, model: str = MODEL,
                    token_count: Callable[[str], int] | None = None) -> tuple[Preflight, dict[str, Any]]:
    if model != MODEL:
        raise PreflightError("Only the explicitly authorized exact model is allowed")
    if token_count is None:
        token_count = _tokenizer(model)
    request = {
        "model": model,
        "instructions": SYSTEM_INSTRUCTION,
        "input": [{"role": "user", "content": json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}],
        "text": {"format": {"type": "json_schema", "name": "outreach_company_analysis",
                             "strict": True, "schema": OPENAI_ANALYSIS_SCHEMA}},
        "reasoning": {"effort": "none"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "tools": [],
        "store": False,
    }
    canonical = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        counted = token_count(canonical)
    except Exception as exc:
        raise PreflightError("Could not count the complete request, instructions, schema, and evidence") from exc
    if not isinstance(counted, int) or counted <= 0:
        raise PreflightError("Tokenizer returned an invalid input count")
    # The extra 256 tokens cover API framing and tokenizer/server boundary
    # variance. We conservatively price the padded count, not the raw count.
    input_estimate = counted + 256
    if input_estimate > MAX_INPUT_TOKENS:
        raise PreflightError(f"Input estimate {input_estimate} exceeds the 15,000-token limit")
    worst = (input_estimate * PRICE_INPUT_PER_MILLION +
             MAX_OUTPUT_TOKENS * PRICE_OUTPUT_PER_MILLION) / 1_000_000
    if not worst < MAX_ESTIMATED_USD:
        raise PreflightError("Worst-case request cost is not below the authorized USD 0.05 cap")
    import hashlib
    result = Preflight(model, input_estimate, MAX_OUTPUT_TOKENS, round(worst, 8),
                       PRICE_INPUT_PER_MILLION, PRICE_OUTPUT_PER_MILLION,
                       PRICING_SOURCE, str(getattr(token_count, "descriptor", "injected-test-counter")),
                       hashlib.sha256(canonical.encode()).hexdigest())
    return result, request


def estimated_cost(input_tokens: int, output_tokens: int) -> float:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("Token counts cannot be negative")
    return round((input_tokens * PRICE_INPUT_PER_MILLION +
                  output_tokens * PRICE_OUTPUT_PER_MILLION) / 1_000_000, 8)


class OpenAIResponsesBackend:
    """One direct request. No SDK, retry middleware, tools, or fallback path."""

    def __init__(self, *, api_key: str | None = None,
                 client_factory: Callable[..., Any] = httpx.Client):
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not key or not key.strip():
            raise CredentialUnavailable("OPENAI_API_KEY is not available to this local process")
        if key != key.strip() or any(char in key for char in "\r\n\"'"):
            raise PreflightError("OPENAI_API_KEY contains surrounding whitespace or quote characters")
        self._key = key
        self._client_factory = client_factory

    def analyze_once(self, evidence: Mapping[str, Any], *, token_count: Callable[[str], int] | None = None,
                     model: str = MODEL) -> dict[str, Any]:
        preflight, request = build_preflight(evidence, model=model, token_count=token_count)
        # fixed endpoint and fixed public price region; no environment-selected
        # base URL, redirects, proxy, tools, or SDK retry behavior.
        with self._client_factory(timeout=httpx.Timeout(20.0, connect=5.0),
                                  follow_redirects=False, trust_env=False) as client:
            response = client.post(API_URL,
                                   headers={"Authorization": f"Bearer {self._key}",
                                            "Content-Type": "application/json"},
                                   json=request)
        status_code = int(response.status_code)
        try:
            raw = response.json()
        except Exception as exc:
            # Keep the bounded body for diagnosis, but distinguish transport
            # JSON decoding from a model-produced structured-output failure.
            body = response.text[:12000]
            raise OpenAIRequestError(status_code, "INVALID_RESPONSE_JSON",
                                     "Provider response body was not valid JSON",
                                     raw_response={"status_code": status_code, "body_text": body},
                                     failure_stage="provider_response_json",
                                     request_id=getattr(response, "headers", {}).get("x-request-id")) from exc
        if status_code < 200 or status_code >= 300:
            err = raw.get("error", {}) if isinstance(raw, dict) else {}
            # Do not save response headers or authorization state.
            raise OpenAIRequestError(status_code, str(err.get("code") or "API_ERROR"),
                                     str(err.get("message") or "OpenAI request failed"), raw_response=raw,
                                     failure_stage="provider_http",
                                     request_id=(raw.get("id") or raw.get("request_id") or
                                                 getattr(response, "headers", {}).get("x-request-id")))
        if not isinstance(raw, dict):
            raise OpenAIRequestError(status_code, "INVALID_RESPONSE", "Response JSON was not an object", raw_response=raw,
                                     failure_stage="provider_response_json",
                                     request_id=getattr(response, "headers", {}).get("x-request-id"))
        usage = raw.get("usage") or {}
        in_tokens = usage.get("input_tokens")
        out_tokens = usage.get("output_tokens")
        if not isinstance(in_tokens, int) or not isinstance(out_tokens, int):
            raise OpenAIRequestError(status_code, "USAGE_MISSING", "Response did not include verifiable token usage", raw_response=raw,
                                     failure_stage="provider_accounting")
        if in_tokens > MAX_INPUT_TOKENS or out_tokens > MAX_OUTPUT_TOKENS:
            raise OpenAIRequestError(status_code, "TOKEN_LIMIT_EXCEEDED", "Server token usage exceeded configured limits", raw_response=raw,
                                     failure_stage="provider_accounting", usage=usage, request_id=raw.get("id"))
        details = usage.get("output_tokens_details") or {}
        reasoning_tokens = details.get("reasoning_tokens", 0)
        if not isinstance(reasoning_tokens, int) or reasoning_tokens < 0 or reasoning_tokens > out_tokens:
            raise OpenAIRequestError(status_code, "INVALID_REASONING_USAGE", "Reasoning token usage was invalid", raw_response=raw,
                                     failure_stage="provider_accounting", usage=usage, request_id=raw.get("id"))
        actual_cost = estimated_cost(in_tokens, out_tokens)
        if not actual_cost < MAX_ESTIMATED_USD:
            raise OpenAIRequestError(status_code, "COST_CAP_EXCEEDED", "Actual token usage reached the authorized cost cap", raw_response=raw,
                                     failure_stage="provider_accounting", usage=usage, request_id=raw.get("id"), actual_cost_usd=actual_cost)
        if raw.get("status") != "completed":
            raise OpenAIRequestError(status_code, "INCOMPLETE_RESPONSE", f"Response status was {raw.get('status')}", raw_response=raw,
                                     failure_stage="provider_completion", usage=usage, request_id=raw.get("id"), actual_cost_usd=actual_cost)
        if raw.get("model") != MODEL or not isinstance(raw.get("id"), str) or not raw.get("id"):
            raise OpenAIRequestError(status_code, "MODEL_OR_REQUEST_ID_MISMATCH", "Response model or request identifier was missing or unexpected", raw_response=raw,
                                     failure_stage="provider_metadata", usage=usage, request_id=raw.get("id"), actual_cost_usd=actual_cost)
        output_text = raw.get("output_text")
        if not isinstance(output_text, str):
            texts = []
            for output_item in raw.get("output", []):
                for content in output_item.get("content", []):
                    if content.get("type") == "output_text":
                        texts.append(content.get("text", ""))
            output_text = "".join(texts)
        try:
            structured = json.loads(output_text)
        except Exception as exc:
            raise OpenAIRequestError(status_code, "INVALID_STRUCTURED_OUTPUT", "Model output was not valid JSON", raw_response=raw,
                                     failure_stage="structured_json_parse", failure_path="$", usage=usage,
                                     request_id=raw.get("id"), actual_cost_usd=actual_cost) from exc
        try:
            _validate_against_schema(structured, OPENAI_ANALYSIS_SCHEMA)
        except (TypeError, ValueError) as exc:
            path_match = re.search(r"\bat \$([^:]*)(?::|$)", str(exc))
            raise OpenAIRequestError(status_code, "STRUCTURED_VALIDATION_FAILED", str(exc), raw_response=raw,
                                     failure_stage="json_schema_validation",
                                     failure_path="$" + path_match.group(1) if path_match else "$",
                                     usage=usage, request_id=raw.get("id"), actual_cost_usd=actual_cost) from exc
        structured, abstention_rejections = normalize_abstained_recommendations(structured)
        structured, rejected_claims = reject_claims_with_unknown_evidence(structured, evidence)
        rejected_claims = abstention_rejections + rejected_claims
        try:
            validate_openai_response(structured, evidence)
        except (TypeError, ValueError) as exc:
            message = str(exc)
            if "identity" in message.casefold() or "business identity" in message.casefold():
                stage = "identity_validation"
            elif "evidence" in message.casefold() or "first-party" in message.casefold() or "non-company" in message.casefold():
                stage = "evidence_reference_validation"
            else:
                stage = "semantic_contract_validation"
            path = "$"
            path_match = re.search(r"\b(Claim [^ ]+|products_services|target_audiences|capabilities_characteristics|Relationship|GEO hypothesis)", message)
            if path_match:
                path = path_match.group(0)
            raise OpenAIRequestError(status_code, "STRUCTURED_VALIDATION_FAILED", message, raw_response=raw,
                                     failure_stage=stage, failure_path=path,
                                     usage=usage, request_id=raw.get("id"), actual_cost_usd=actual_cost) from exc
        return {
            "provider": "openai", "model": str(raw.get("model") or MODEL),
            "request_id": str(raw.get("id") or ""), "status": raw.get("status"),
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                      "reasoning_tokens": reasoning_tokens,
                      "reasoning_tokens_reported": "reasoning_tokens" in details},
            "estimated_cost_usd": actual_cost,
            "preflight": preflight.__dict__, "raw_response": raw,
            "structured_output": structured, "rejected_claims": rejected_claims,
        }


class OpenAIRequestError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, *, raw_response: Any = None,
                 failure_stage: str = "provider_request", failure_path: str | None = None,
                 usage: Mapping[str, Any] | None = None, request_id: str | None = None,
                 actual_cost_usd: float | None = None):
        # Scrub common API-key shapes in unexpected provider diagnostics.
        message = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", message)
        self.status_code, self.code, self.message = status_code, code, message[:500]
        self.raw_response = raw_response
        self.failure_stage = failure_stage
        self.failure_path = failure_path
        self.usage = dict(usage or {})
        raw_request_id = (raw_response.get("id") or raw_response.get("request_id")
                          if isinstance(raw_response, dict) else None)
        self.request_id = request_id or raw_request_id
        self.actual_cost_usd = actual_cost_usd
        super().__init__(f"OpenAI request failed ({status_code}, {code}): {self.message}")

    def diagnostic_artifact(self) -> dict[str, Any]:
        """Response artifact for the admin-only attempt ledger; never includes request headers."""
        def redact(value: Any) -> Any:
            if isinstance(value, str):
                value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
                value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1[REDACTED]", value)
                return value
            if isinstance(value, dict):
                return {str(key): redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        raw = redact(self.raw_response)
        if not isinstance(raw, (dict, list, str, int, float, bool, type(None))):
            raw = {"unserializable_response_type": type(raw).__name__}
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded) > 256_000:
            raw = {"truncated": True, "sha256": __import__("hashlib").sha256(encoded.encode()).hexdigest(),
                   "preview": encoded[:4096]}
        return {
            "diagnostic_version": 1,
            "http_status": self.status_code,
            "request_id": self.request_id,
            "model": raw.get("model") if isinstance(raw, dict) else None,
            "response_status": raw.get("status") if isinstance(raw, dict) else None,
            "incomplete_details": raw.get("incomplete_details") if isinstance(raw, dict) else None,
            "finish_reason": raw.get("finish_reason") if isinstance(raw, dict) else None,
            "usage": self.usage,
            "actual_cost_usd": self.actual_cost_usd,
            "failure_code": self.code,
            "failure_stage": self.failure_stage,
            "failure_path": self.failure_path,
            "failure_message": self.message,
            "provider_response": raw,
        }


def attempt_failure_accounting(exc: OpenAIRequestError, previous: Mapping[str, Any]) -> dict[str, Any]:
    """Attempt-row fields for a failed local validation after a provider call.

    Omitting ``reserved_usd`` is intentional: unknown spend keeps the existing
    reservation untouched. A verified usage/cost closes that reservation and
    records the actual charge.
    """
    from decimal import Decimal
    known_cost = exc.actual_cost_usd
    values: dict[str, Any] = {
        "status": ("VALIDATION_FAILED" if exc.failure_stage in {
            "structured_json_parse", "json_schema_validation", "identity_validation",
            "evidence_reference_validation", "semantic_contract_validation"}
            else "PROVIDER_FAILED") if known_cost is not None else "UNKNOWN_SPEND",
        "request_id": exc.request_id,
        "usage": json.dumps(exc.usage, ensure_ascii=False, separators=(",", ":")) if exc.usage
                 else previous.get("usage") or "{}",
        "actual_cost_usd": format(Decimal(str(known_cost)), "f") if known_cost is not None
                           else previous.get("actual_cost_usd"),
        "provider_response": json.dumps(exc.diagnostic_artifact(), ensure_ascii=False,
                                         separators=(",", ":"), default=str),
        "error_code": exc.code,
    }
    return values


def _validate_against_schema(value: Any, schema: Mapping[str, Any], path: str = "$" ) -> None:
    kind = schema.get("type")
    valid = {
        "object": lambda x: isinstance(x, dict),
        "array": lambda x: isinstance(x, list),
        "string": lambda x: isinstance(x, str),
        "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
        "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
        "boolean": lambda x: isinstance(x, bool),
    }.get(kind)
    if valid and not valid(value):
        raise ValueError(f"Response schema type mismatch at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Response schema enum mismatch at {path}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"Response schema constant mismatch at {path}")
    if kind == "object":
        properties = schema.get("properties", {})
        missing = set(schema.get("required", ())) - set(value)
        if missing:
            raise ValueError(f"Response missing required fields at {path}")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"Response has unexpected fields at {path}")
        for name, child in properties.items():
            if name in value:
                _validate_against_schema(value[name], child, f"{path}.{name}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"Response array too short at {path}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"Response array too long at {path}")
        if schema.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            raise ValueError(f"Response array items must be unique at {path}")
        for index, child in enumerate(value):
            _validate_against_schema(child, schema["items"], f"{path}[{index}]")


def reject_claims_with_unknown_evidence(raw: Mapping[str, Any], evidence: Mapping[str, Any]
                                        ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove unsupported individual claims while retaining supported analysis.

    This is deliberately claim-scoped: a bad citation or unsupported numeric/
    comparative assertion does not invalidate unrelated claims. Entity identity
    remains a hard validation requirement, and retained entries still pass the
    strict schema, URL, attribution, and evidence-ID checks below.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("Model response must be an object")
    supplied = {str(item.get("id")): item for item in evidence.get("facts", [])}
    host = _host(str(evidence.get("website") or ""))
    if not host:
        raise ValueError("Evidence website is invalid")
    allowed_business = {"TARGET_BUSINESS", "PRODUCT_OR_SERVICE"}
    rejected: list[dict[str, Any]] = []

    def filter_entries(key: str, *, entry_type: str, business_claim: bool,
                       product_service: bool = False) -> None:
        entries = raw.get(key)
        if not isinstance(entries, list):
            return  # strict schema validation below reports malformed containers
        accepted = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                accepted.append(entry)  # preserve for the schema validator
                continue
            refs = entry.get("evidence_ids", [])
            refs = refs if isinstance(refs, list) else []
            unknown = [str(ref) for ref in refs if str(ref) not in supplied]
            reasons: list[str] = []
            if not refs:
                reasons.append("MISSING_EVIDENCE_ID")
            if unknown:
                reasons.append("UNKNOWN_EVIDENCE_ID")
            for ref in refs:
                fact = supplied.get(str(ref))
                if not fact:
                    continue
                if _host(str(fact.get("source_url") or "")) != host:
                    reasons.append("NON_FIRST_PARTY_EVIDENCE")
                classification = str(fact.get("classification") or "")
                if business_claim and classification not in allowed_business:
                    reasons.append("NON_COMPANY_EVIDENCE")
            if product_service and not any(
                    str(supplied[str(ref)].get("classification") or "") == "PRODUCT_OR_SERVICE"
                    for ref in refs if str(ref) in supplied):
                reasons.append("NOT_PRODUCT_OR_SERVICE_EVIDENCE")
            text = str(entry.get("text") or entry.get("summary") or
                       entry.get("description") or entry.get("hypothesis") or
                       entry.get("observation") or "")
            if entry_type == "PROPOSED_CLAIM" and not reasons:
                excerpts = " ".join(str(supplied[str(ref)].get("exact_excerpt") or "") for ref in refs)
                number_pattern = r"(?:\$\s?\d+(?:,\d{3})*(?:\.\d+)?|\b\d+(?:\.\d+)?%?\b)"
                for number in re.findall(number_pattern, text):
                    if number.casefold().replace(" ", "") not in excerpts.casefold().replace(" ", ""):
                        reasons.append("UNSUPPORTED_NUMERICAL_CLAIM")
                        break
                if not reasons:
                    for match in _UNSUPPORTED_COMPARATIVE_RE.finditer(text):
                        if not re.search(rf"\b{re.escape(match.group(0))}\b", excerpts, re.I):
                            reasons.append("UNSUPPORTED_COMPARATIVE_CLAIM")
                            break
            if reasons:
                status = "REJECTED_" + reasons[0]
                rejected.append({"entry_type": entry_type,
                                 "id": str(entry.get("id") or ""),
                                 "text": text,
                                 "evidence_ids": [str(ref) for ref in refs],
                                 "unknown_evidence_ids": unknown,
                                 "reason_codes": list(dict.fromkeys(reasons)),
                                 "status": status})
            else:
                accepted.append(entry)
        # Copy-on-write so the archived raw response remains untouched.
        raw_copy[key] = accepted

    raw_copy = dict(raw)
    filter_entries("proposed_claims", entry_type="PROPOSED_CLAIM", business_claim=False)
    filter_entries("products_services", entry_type="PRODUCT_OR_SERVICE", business_claim=True)
    filter_entries("target_audiences", entry_type="TARGET_AUDIENCE", business_claim=True)
    filter_entries("capabilities_characteristics", entry_type="CAPABILITY", business_claim=True)
    filter_entries("observed_relationships", entry_type="OBSERVED_RELATIONSHIP", business_claim=False)
    known_relationships = {str(x.get("id")) for x in raw_copy.get("observed_relationships", [])
                           if isinstance(x, Mapping)}
    filter_entries("potential_geo_implications", entry_type="GEO_HYPOTHESIS", business_claim=False)
    hypotheses = []
    for item in raw_copy.get("potential_geo_implications", []):
        if not isinstance(item, Mapping):
            hypotheses.append(item)
            continue
        unknown_relations = [str(x) for x in item.get("relationship_ids", [])
                             if str(x) not in known_relationships]
        if unknown_relations:
            rejected.append({"entry_type": "GEO_HYPOTHESIS", "id": str(item.get("id") or ""),
                "text": str(item.get("hypothesis") or ""),
                "evidence_ids": [str(x) for x in item.get("evidence_ids", [])],
                "unknown_relationship_ids": unknown_relations,
                "reason_codes": ["UNKNOWN_RELATIONSHIP_ID"], "status": "REJECTED_UNKNOWN_RELATIONSHIP_ID"})
        else:
            hypotheses.append(item)
    raw_copy["potential_geo_implications"] = hypotheses
    if raw_copy.get("recommendation_decision") == "RECOMMEND" and not hypotheses:
        raw_copy["recommendation_decision"] = "ABSTAIN"
    return raw_copy, rejected


def normalize_abstained_recommendations(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Honor an explicit GEO ABSTAIN while excluding its contradictory hypotheses."""
    normalized = dict(raw)
    hypotheses = normalized.get("potential_geo_implications")
    if normalized.get("recommendation_decision") != "ABSTAIN" or not hypotheses:
        return normalized, []
    normalized["potential_geo_implications"] = []
    rejected = [{"id": str(item.get("id") or ""),
                 "text": str(item.get("hypothesis") or item.get("recommendation") or ""),
                 "evidence_ids": list(item.get("evidence_ids") or []),
                 "status": "EXCLUDED_INCONSISTENT_WITH_ABSTAIN",
                 "reason": "The model explicitly abstained; GEO hypotheses were excluded from analysis."}
                for item in hypotheses if isinstance(item, Mapping)]
    return normalized, rejected


def validate_openai_response(raw: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
    """Independent structural/provenance checks, not semantic entailment."""
    if not isinstance(raw, Mapping):
        raise ValueError("Model response must be an object")
    _validate_against_schema(raw, OPENAI_ANALYSIS_SCHEMA)
    if raw.get("version") != CONTRACT_VERSION:
        raise ValueError("Model response contract version mismatch")
    supplied = {str(item.get("id")): item for item in evidence.get("facts", [])}
    allowed_company_categories = {"TARGET_BUSINESS", "PRODUCT_OR_SERVICE"}
    claims = raw.get("proposed_claims")
    if not isinstance(claims, list):
        raise ValueError("proposed_claims must be an array")
    ids: set[str] = set()
    host = _host(str(evidence.get("website") or ""))
    identity = raw.get("entity_identity") or {}
    if str(identity.get("name") or "").casefold() != str(evidence.get("target_company") or "").casefold():
        raise ValueError("Model changed the target business identity")
    if _host(str(identity.get("website") or "")) != host:
        raise ValueError("Model changed the verified website identity")
    if not identity.get("evidence_ids") or any(str(ref) not in supplied for ref in identity["evidence_ids"]):
        raise ValueError("Business identity contains missing or unknown evidence IDs")
    for claim in claims:
        if not isinstance(claim, Mapping):
            raise ValueError("Claim must be an object")
        cid = str(claim.get("id") or "")
        if not cid or cid in ids:
            raise ValueError("Claim IDs must be nonempty and unique")
        ids.add(cid)
        refs = claim.get("evidence_ids")
        if not isinstance(refs, list) or not refs:
            raise ValueError(f"Claim {cid} has no evidence IDs")
        role = claim.get("role")
        for ref in refs:
            fact = supplied.get(str(ref))
            if not fact:
                raise ValueError(f"Claim {cid} references unknown evidence {ref}")
            if _host(str(fact.get("source_url") or "")) != host:
                raise ValueError(f"Claim {cid} cites a non-first-party URL")
            if role != "GEO_OBSERVATION" and fact.get("classification") not in allowed_company_categories:
                raise ValueError(f"Claim {cid} promotes non-company evidence to a business claim")
        evidence_text = " ".join(supplied[str(ref)].get("exact_excerpt", "") for ref in refs)
        claim_text = str(claim.get("text") or "")
        number_pattern = r"(?:\$\s?\d+(?:,\d{3})*(?:\.\d+)?|\b\d+(?:\.\d+)?%?\b)"
        for value in re.findall(number_pattern, claim_text):
            if value.casefold().replace(" ", "") not in evidence_text.casefold().replace(" ", ""):
                raise ValueError(f"Claim {cid} introduces an unsupported numerical claim")
        for match in _UNSUPPORTED_COMPARATIVE_RE.finditer(claim_text):
            comparative = match.group(0)
            if not re.search(rf"\b{re.escape(comparative)}\b", evidence_text, re.I):
                raise ValueError(f"Claim {cid} introduces an unsupported comparative claim")
        # Claim entailment, contradiction, and numerical/comparative validity
        # are deliberately left for a human; the output audit below records
        # that limitation instead of treating model labels as verification.
    for key in ("products_services", "target_audiences", "capabilities_characteristics"):
        for item in raw.get(key, []):
            refs = item.get("evidence_ids", [])
            if not refs or any(str(ref) not in supplied for ref in refs):
                raise ValueError(f"{key} contains missing or unknown evidence IDs")
            if any(supplied[str(ref)].get("classification") not in allowed_company_categories for ref in refs):
                raise ValueError(f"{key} promotes non-company evidence into a business fact")
    relationship_ids = {str(x.get("id")) for x in raw.get("observed_relationships", [])}
    for relationship in raw.get("observed_relationships", []):
        if not relationship.get("evidence_ids") or any(str(x) not in supplied for x in relationship["evidence_ids"]):
            raise ValueError("Relationship contains missing or unknown evidence IDs")
    for hypothesis in raw.get("potential_geo_implications", []):
        if any(str(x) not in supplied for x in hypothesis.get("evidence_ids", [])):
            raise ValueError("GEO hypothesis contains unknown evidence IDs")
        if any(str(x) not in relationship_ids for x in hypothesis.get("relationship_ids", [])):
            raise ValueError("GEO hypothesis refers to an unknown relationship")
    if raw.get("recommendation_decision") == "RECOMMEND" and not raw.get("potential_geo_implications"):
        raise ValueError("Model recommended without a cited GEO hypothesis")
    if raw.get("recommendation_decision") == "ABSTAIN" and raw.get("potential_geo_implications"):
        raise ValueError("Model abstained while returning GEO hypotheses; inconsistent response")


def replay_saved_provider_attempt(previous: Mapping[str, Any], evidence: Mapping[str, Any],
                                  preflight: Preflight) -> dict[str, Any]:
    """Revalidate a known-cost provider response offline; never issue a request.

    The evidence fingerprint is checked by the caller before this function is
    reached. Only complete provider responses with verifiable token usage and
    a matching persisted actual cost are eligible for replay.
    """
    from decimal import Decimal

    status = str(previous.get("status") or "")
    if status not in {"COMPLETED", "VALIDATION_FAILED"}:
        raise ValueError("Saved provider attempt is not eligible for offline replay")
    try:
        saved = json.loads(previous.get("provider_response") or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("Saved provider response is not valid JSON") from exc
    if not isinstance(saved, Mapping):
        raise ValueError("Saved provider response is not an object")
    diagnostic = saved
    raw = saved.get("provider_response") if status == "VALIDATION_FAILED" else saved.get("raw_response")
    # Older COMPLETED replay rows wrapped the API response in provider_response.
    # Accept the envelope only if every ledger, model, usage, cost, schema,
    # identity and evidence check below passes.
    if not isinstance(raw, Mapping) and status == "COMPLETED":
        raw = saved.get("provider_response")
    if (status == "COMPLETED" and isinstance(raw, Mapping)
            and raw.get("status") is None and isinstance(raw.get("provider_response"), Mapping)):
        raw = raw["provider_response"]
    if not isinstance(raw, Mapping):
        raise ValueError("Saved response does not contain the raw provider response")
    if raw.get("status") != "completed" or raw.get("model") != MODEL:
        raise ValueError("Saved provider response is incomplete or uses a different model")
    request_id = str(raw.get("id") or "")
    if not request_id or (previous.get("request_id") and request_id != str(previous["request_id"])):
        raise ValueError("Saved provider request ID does not match its ledger row")
    usage = raw.get("usage") or diagnostic.get("usage") or {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        raise ValueError("Saved provider response has no verifiable token usage")
    cost = estimated_cost(input_tokens, output_tokens)
    try:
        recorded_cost = Decimal(str(previous.get("actual_cost_usd")))
        diagnostic_cost = diagnostic.get("actual_cost_usd")
        if diagnostic_cost is not None and Decimal(str(diagnostic_cost)) != recorded_cost:
            raise ValueError("Saved diagnostic cost does not match its ledger row")
    except Exception as exc:
        raise ValueError("Saved provider cost cannot be verified") from exc
    if not recorded_cost.is_finite() or recorded_cost <= 0 or Decimal(str(cost)) != recorded_cost:
        raise ValueError("Saved provider token usage does not match the known ledger cost")
    details = usage.get("output_tokens_details") or {}
    reasoning_tokens = details.get("reasoning_tokens", 0)
    if not isinstance(reasoning_tokens, int) or reasoning_tokens < 0 or reasoning_tokens > output_tokens:
        raise ValueError("Saved provider reasoning-token usage is invalid")
    output_text = raw.get("output_text")
    if not isinstance(output_text, str):
        chunks = []
        for output_item in raw.get("output", []):
            for content in output_item.get("content", []):
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
        output_text = "".join(chunks)
    try:
        structured = json.loads(output_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Saved provider output is not valid structured JSON") from exc
    _validate_against_schema(structured, OPENAI_ANALYSIS_SCHEMA)
    structured, abstention_rejections = normalize_abstained_recommendations(structured)
    structured, rejected = reject_claims_with_unknown_evidence(structured, evidence)
    validate_openai_response(structured, evidence)
    prior_rejected = saved.get("rejected_claims") if status == "COMPLETED" else []
    rejected = list(prior_rejected or []) + abstention_rejections + rejected
    return {
        "provider": "openai", "model": MODEL, "request_id": request_id,
        "status": "completed",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                  "reasoning_tokens": reasoning_tokens,
                  "reasoning_tokens_reported": "reasoning_tokens" in details},
        "estimated_cost_usd": float(recorded_cost), "preflight": preflight.__dict__,
        "raw_response": raw, "structured_output": structured,
        "rejected_claims": rejected, "offline_replay": True,
    }


def to_evidence_contract(raw: Mapping[str, Any], selection: EvidenceSelection) -> EvidenceContract:
    """Attach server-owned sources and replace model self-reviews with pending review."""
    source_facts = tuple(SourceFact(
        id=str(item["id"]), text=str(item["text"]), exact_excerpt=str(item["exact_excerpt"]),
        source_url=str(item["source_url"]),
        source_content=str(item.get("source_content") or ""),
        source_title=str(item.get("source_title") or ""),
        section=str(item.get("section") or ""), attribution=str(item.get("attribution") or ""),
        observed_at="",
    ) for item in selection.facts)
    claims = tuple(ProposedClaim(str(x["id"]), str(x["text"]), tuple(x["evidence_ids"]),
                                 str(x["attribution"]), str(x["role"]))
                   for x in raw["proposed_claims"])
    reviews = tuple(ClaimReview(
        claim_id=x.id, status=SupportStatus.NOT_VERIFIABLE_FROM_SAMPLE,
        rationale="Structural provenance passed; automated citations do not prove semantic entailment. Recipient copy may reuse the cited model wording and remains subject to human review.",
        evidence_ids=x.evidence_ids, reviewer="AUTOMATED_FILTERING_ONLY") for x in claims)
    relationships = tuple(SiteRelationship(
        id=str(x["id"]), description=str(x["description"]),
        evidence_ids=tuple(x["evidence_ids"]), kind=str(x.get("kind") or "OBSERVED_RELATIONSHIP"))
        for x in raw["observed_relationships"])
    hypotheses = tuple(GeoHypothesis(
        id=str(x["id"]), hypothesis=str(x["hypothesis"]), evidence_ids=tuple(x["evidence_ids"]),
        relationship_ids=tuple(x["relationship_ids"]), status="HYPOTHESIS_REQUIRES_REVIEW")
        for x in raw["potential_geo_implications"])
    contract = EvidenceContract(
        version=CONTRACT_VERSION,
        entity_identity=dict(raw["entity_identity"]),
        products_services=tuple(raw["products_services"]),
        target_audiences=tuple(raw["target_audiences"]),
        capabilities_characteristics=tuple(raw["capabilities_characteristics"]),
        source_facts=source_facts, proposed_claims=claims,
        claim_to_evidence={x.id: x.evidence_ids for x in claims}, claim_reviews=reviews,
        evidence_limitations=tuple(raw["evidence_limitations"]),
        observed_relationships=relationships, potential_geo_implications=hypotheses,
        unsupported_or_unresolved_claims=tuple(raw["unsupported_or_unresolved_claims"]),
        final_personalization_claim_ids=(),
        recommendation_decision="RECOMMEND" if raw["recommendation_decision"] == "RECOMMEND" else "ABSTAIN",
    )
    errors = validate_contract(contract)
    if errors:
        raise ValueError("Normalized semantic analysis failed contract validation: " + ",".join(errors))
    return contract


def build_automated_personalization(raw: Mapping[str, Any], selection: EvidenceSelection,
                                   *, stamp: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compose reviewable invitation content from validated first-party facts.

    The model response guides which cited business description and offering
    are used. Recipient-facing facts come from the application's extracted
    source statements. Model claim prose remains in the audit and is not
    treated as semantically proven by its citations.
    """
    evidence = selection.to_input()
    _validate_against_schema(raw, OPENAI_ANALYSIS_SCHEMA)
    raw, abstention_rejections = normalize_abstained_recommendations(raw)
    validate_openai_response(raw, evidence)
    facts = {str(item["id"]): item for item in selection.facts}
    claims = list(raw.get("proposed_claims") or [])
    audit: list[dict[str, Any]] = []
    for claim in claims:
        refs = [str(value) for value in claim.get("evidence_ids", [])]
        cited = [facts.get(ref) for ref in refs]
        excerpt = " ".join(item.get("exact_excerpt", "") for item in cited if item)
        text = str(claim.get("text") or "")
        unsupported_comparison = _has_unsupported_comparative(text) or bool(re.search(
            r"\b(?:not\s+just|more\s+than|less\s+than|better\s+than|worse\s+than)\b", text, re.I))
        supported_comparison = _has_unsupported_comparative(excerpt) or bool(re.search(
            r"\b(?:not\s+just|more\s+than|less\s+than|better\s+than|worse\s+than)\b", excerpt, re.I))
        if unsupported_comparison and not supported_comparison:
            status = "EXCLUDED_UNSUPPORTED_COMPARISON"
            reason = "The cited first-party excerpts do not establish the comparative baseline or superlative."
        else:
            status = "AUDIT_ONLY_NOT_USED_AS_APPROVED_COPY"
            reason = "Model claim text is preserved, but recipient copy is composed from validated extracted source facts."
        audit.append({"id": str(claim.get("id") or ""), "role": str(claim.get("role") or ""), "original_text": text,
                      "evidence_ids": refs, "status": status, "reason": reason,
                      "deterministic_transformation": None})
    for item in abstention_rejections:
        audit.append({"id": item["id"], "role": "GEO_HYPOTHESIS",
                      "original_text": item["text"],
                      "evidence_ids": item["evidence_ids"],
                      "status": item["status"], "reason": item["reason"],
                      "deterministic_transformation": "Excluded because recommendation_decision=ABSTAIN"})

    rejected = []
    # Business identity is selected only from the model-cited, target-business
    # facts. The emitted description is the source-derived statement, not a
    # paraphrase of the model's proposed claim.
    identity_refs = [str(value) for value in raw["entity_identity"]["evidence_ids"]]
    description_fact = next((facts[ref] for ref in identity_refs
                             if ref in facts
                             and facts[ref].get("classification") == "TARGET_BUSINESS"
                             and facts[ref].get("assertion_type") == "ATTRIBUTED_COMPANY_DESCRIPTION"), None)
    if description_fact is None:
        description_fact = next((facts[ref] for ref in identity_refs
                                 if ref in facts and facts[ref].get("classification") == "TARGET_BUSINESS"), None)
    description_claim = None
    if description_fact is None:
        # A response may cite a first-party offering for both the exact entity
        # identity and its concise business-description claim. Keep the exact
        # name/domain checks above; only recover copy from a cited description
        # whose references overlap identity evidence and remain same-site facts.
        for claim in claims:
            refs = [str(value) for value in claim.get("evidence_ids", [])]
            text = str(claim.get("text") or "").strip()
            cited_identity = [ref for ref in refs if ref in identity_refs and ref in facts]
            if (claim.get("role") == "BUSINESS_DESCRIPTION" and text and cited_identity
                    and len(text) <= 500
                    and all(ref in facts and facts[ref].get("classification")
                            in {"TARGET_BUSINESS", "PRODUCT_OR_SERVICE"} for ref in refs)):
                description_fact = facts[cited_identity[0]]
                description_claim = claim
                break
    if description_fact is None:
        raise ValueError("No model-cited, first-party target-business description is available")

    # Use the first model-selected service whose citation resolves to a
    # first-party product/service fact. The wording emitted below is
    # the extractor's concise statement attached to that exact source.
    service_fact = None
    service_summary = None
    service_summary_refs = None
    for entry in raw.get("products_services", []):
        refs = [str(value) for value in entry.get("evidence_ids", [])]
        summary = str(entry.get("summary") or "").strip()
        if not summary or len(summary) > 260 or any(ref not in facts for ref in refs):
            continue
        candidates = [facts.get(ref) for ref in refs]
        candidates = [item for item in candidates if item
                      and item.get("attribution") in {"COMPANY_STATED", "PUBLISHED_STRUCTURED_DATA"}
                      and _direct_offering_fact(item, company_name=selection.target_company)]
        for item in candidates:
            statement = str(item.get("text") or "").strip()
            if statement:
                service_fact = item
                service_summary = summary
                break
        if service_fact:
            break
    if service_fact is None:
        # Some structured outputs omit products_services but still cite an
        # explicit offering in a BUSINESS_DESCRIPTION or
        # COMPANY_CHARACTERISTIC claim. Recover only from those cited claims;
        # never infer a service from uncited page text.
        for claim in claims:
            if claim.get("role") not in {"BUSINESS_DESCRIPTION", "COMPANY_CHARACTERISTIC"}:
                continue
            refs = [str(value) for value in claim.get("evidence_ids", [])]
            cited = [facts.get(ref) for ref in refs]
            supported = [item for item in cited if item
                         and item.get("attribution") in {"COMPANY_STATED", "PUBLISHED_STRUCTURED_DATA"}
                         and _direct_offering_fact(item, company_name=selection.target_company)]
            text = str(claim.get("text") or "").strip()
            if refs and supported and len(text) <= 260 and all(item is not None for item in cited):
                service_fact = supported[0]
                service_summary = text
                service_summary_refs = refs
                break
    if service_fact is None:
        raise ValueError("No concise, cited first-party service statement is available")

    company = selection.target_company
    description = str(description_fact["text"]).strip()
    strength = service_summary
    service_assertion = str(service_fact["text"]).strip().rstrip(".")
    if service_assertion:
        service_assertion = service_assertion[0].lower() + service_assertion[1:]
    # Use the concise semantic analysis for recipient copy when the response
    # cites the exact source facts. Keep extracted source statements in the
    # evidence profile, but avoid copying long website passages verbatim.
    def claim_copy(role: str, required_fact_id: str | None = None):
        for claim in claims:
            refs = [str(value) for value in claim.get("evidence_ids", [])]
            text = str(claim.get("text") or "").strip()
            if (claim.get("role") == role and text and refs and
                    (required_fact_id is None or required_fact_id in refs) and
                    all(ref in facts for ref in refs)):
                return text, refs
        return None, None

    summary, summary_refs = claim_copy("BUSINESS_DESCRIPTION", str(description_fact["id"]))
    if not summary and description_claim is not None:
        summary = str(description_claim.get("text") or "").strip()
        summary_refs = [str(value) for value in description_claim.get("evidence_ids", [])]
    if not summary:
        summary, summary_refs = claim_copy("BUSINESS_DESCRIPTION")
    if not summary:
        summary, summary_refs = description, [str(description_fact["id"])]
    detail, detail_refs = None, None
    for entry in raw.get("products_services", []):
        refs = [str(value) for value in entry.get("evidence_ids", [])]
        text = str(entry.get("summary") or "").strip()
        if service_fact["id"] in refs and text and all(ref in facts for ref in refs):
            detail, detail_refs = text, refs
            break
    if not detail:
        detail = strength
        detail_refs = service_summary_refs or [str(service_fact["id"])]
    relevance, relevance_refs = claim_copy("RESEARCH_RELEVANCE")
    if not relevance:
        relevance = (
            f"The site's description of {company} alongside the specific detail that {service_assertion} "
            "makes it a useful case for studying how AI-powered search connects a business identity with its offerings."
        )
        relevance_refs = [str(description_fact["id"]), str(service_fact["id"])]
    # Keep every fetched page in the provenance set. Some sampled pages may
    # yield no eligible fact, but remain part of the recorded evidence limits.
    all_urls = list(dict.fromkeys(str(page.get("url")) for page in selection.pages if page.get("url")))
    profile_facts = [{
        "id": str(item["id"]), "statement": str(item["text"]),
        "evidence_excerpt": str(item["exact_excerpt"]),
        "source_content": str(item.get("source_content") or ""),
        "source_urls": [str(item["source_url"])], "source_title": str(item.get("source_title") or ""),
        "section_heading": str(item.get("section") or ""),
        "assertion_type": str(item.get("assertion_type") or ""),
        "attribution": str(item.get("attribution") or ""),
        "classification": str(item.get("classification") or ""),
    } for item in selection.facts]
    personalization = {
        "analysis_version": "structured-evidence-v2", "data_classification": "OBSERVED",
        "business_description": description, "company_strength": strength,
        "introduction": "We are conducting a university study about how AI-powered search represents businesses and their offerings.",
        "recipient_copy": {
            "business_summary": summary,
            "distinctive_detail": detail,
            "research_relevance": relevance,
            "supported_observation": "",
            "support": {"business_summary": summary_refs,
                        "distinctive_detail": detail_refs,
                        "research_relevance": relevance_refs},
        },
        "source_urls": all_urls, "retrieved_at": stamp, "recommendations": [],
        "assessment": {
            "personalization_evidence": "AUTOMATIC_SOURCE_GROUNDED_COMPOSITION",
            "geo_observation": "NOT_ESTABLISHED", "invitation": "PREVIEW_ONLY",
            "claim_by_claim_approval_required": False,
            "final_invitation_review_required_before_sending": True,
            "limitations": list(selection.limitations),
            "excluded_model_claims": [
                {"id": item["id"], "status": item["status"], "reason": item["reason"]}
                for item in audit if item["status"] == "EXCLUDED_UNSUPPORTED_COMPARISON"
            ],
            "excluded_model_recommendations": abstention_rejections,
            "interpretation": "Recipient wording uses the semantic analysis and its evidence IDs; extracted source facts remain separately available for review.",
        },
        "claim_support": {"business_description": [str(description_fact["id"])],
                          "company_strength": [str(service_fact["id"]) ]},
        "evidence_profile": {"pages": [dict(page) for page in selection.pages],
                             "facts": profile_facts, "relationships": []},
        "evidence": {"sampled_urls": all_urls, "limitations": list(selection.limitations)},
    }
    for item in audit:
        if item["role"] == "BUSINESS_DESCRIPTION":
            if summary_refs and item["original_text"] == summary:
                item["status"] = "USED_IN_RECIPIENT_COPY_WITH_EVIDENCE_REFERENCES"
                item["reason"] = "Recipient summary uses this cited semantic claim; citation validity is not independent entailment verification."
                item["deterministic_transformation"] = {"evidence_ids": summary_refs, "text": summary}
            else:
                item["status"] = "REPLACED_WITH_DIRECT_SOURCE_DESCRIPTION"
                item["reason"] = "No validated concise business-description claim was selected for recipient copy."
                item["deterministic_transformation"] = {"evidence_id": str(description_fact["id"]),
                                                        "text": description}
        elif item["role"] == "RESEARCH_RELEVANCE":
            if relevance_refs and item["original_text"] == relevance:
                item["status"] = "USED_IN_RECIPIENT_COPY_WITH_EVIDENCE_REFERENCES"
                item["reason"] = "Recipient relevance uses this cited semantic claim; citation validity is not independent entailment verification."
                item["deterministic_transformation"] = {"evidence_ids": relevance_refs, "text": relevance}
            else:
                item["status"] = "RECOMPOSED_FROM_CITED_SOURCE_FACTS"
                item["reason"] = "No validated concise research-relevance claim was selected for recipient copy."
                item["deterministic_transformation"] = {"evidence_ids": [str(description_fact["id"]),
                                                                              str(service_fact["id"])],
                                                        "text": relevance}
    return personalization, audit


