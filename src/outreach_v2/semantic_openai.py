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
from dataclasses import dataclass
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


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _classify_fact(item: Mapping[str, Any], site_host: str, company_name: str) -> str:
    urls = item.get("source_urls") or []
    if not urls or any(_host(str(url)) != site_host for url in urls):
        return "THIRD_PARTY_OR_PARTNER"
    raw = " ".join(str(item.get(k) or "") for k in
                   ("attribution", "assertion_type", "relationship", "section_heading")).upper()
    excerpt = str(item.get("evidence_excerpt") or item.get("statement") or "")
    if any(word in raw for word in ("TESTIMONIAL", "CUSTOMER", "PARTNER", "THIRD_PARTY", "PARTICIPANT")):
        return "CUSTOMER_OR_TESTIMONIAL" if "TESTIMONIAL" in raw or "CUSTOMER" in raw else "THIRD_PARTY_OR_PARTNER"
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
        return "TARGET_BUSINESS" if field in {"name", "description", "address", "openinghours", "email", "telephone"} else "AMBIGUOUS"
    if any(word in path for word in ("service", "product", "collection")):
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
    excluded_reasons: dict[str, str] = {}
    from .quality import CODE_MARKERS
    for index, item in enumerate(raw_facts):
        if not isinstance(item, Mapping):
            continue
        excerpt = " ".join(str(item.get("evidence_excerpt") or item.get("statement") or "").split())
        urls = item.get("source_urls") or []
        fact_id = str(item.get("id") or f"candidate_{index}")
        classification = _classify_fact(item, site_host, company_name)
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
                             tuple(str(x) for x in ((personalization.get("assessment") or {}).get("limitations") or [])))


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
        except Exception:
            raw = {"status_code": status_code, "body_text": response.text[:2000]}
        if status_code < 200 or status_code >= 300:
            err = raw.get("error", {}) if isinstance(raw, dict) else {}
            # Do not save response headers or authorization state.
            raise OpenAIRequestError(status_code, str(err.get("code") or "API_ERROR"),
                                     str(err.get("message") or "OpenAI request failed"), raw_response=raw)
        if not isinstance(raw, dict):
            raise OpenAIRequestError(status_code, "INVALID_RESPONSE", "Response JSON was not an object", raw_response=raw)
        usage = raw.get("usage") or {}
        in_tokens = usage.get("input_tokens")
        out_tokens = usage.get("output_tokens")
        if not isinstance(in_tokens, int) or not isinstance(out_tokens, int):
            raise OpenAIRequestError(status_code, "USAGE_MISSING", "Response did not include verifiable token usage", raw_response=raw)
        if in_tokens > MAX_INPUT_TOKENS or out_tokens > MAX_OUTPUT_TOKENS:
            raise OpenAIRequestError(status_code, "TOKEN_LIMIT_EXCEEDED", "Server token usage exceeded configured limits", raw_response=raw)
        details = usage.get("output_tokens_details") or {}
        reasoning_tokens = details.get("reasoning_tokens", 0)
        if not isinstance(reasoning_tokens, int) or reasoning_tokens < 0 or reasoning_tokens > out_tokens:
            raise OpenAIRequestError(status_code, "INVALID_REASONING_USAGE", "Reasoning token usage was invalid", raw_response=raw)
        actual_cost = estimated_cost(in_tokens, out_tokens)
        if not actual_cost < MAX_ESTIMATED_USD:
            raise OpenAIRequestError(status_code, "COST_CAP_EXCEEDED", "Actual token usage reached the authorized cost cap", raw_response=raw)
        if raw.get("status") != "completed":
            raise OpenAIRequestError(status_code, "INCOMPLETE_RESPONSE", f"Response status was {raw.get('status')}", raw_response=raw)
        if raw.get("model") != MODEL or not isinstance(raw.get("id"), str) or not raw.get("id"):
            raise OpenAIRequestError(status_code, "MODEL_OR_REQUEST_ID_MISMATCH", "Response model or request identifier was missing or unexpected", raw_response=raw)
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
            raise OpenAIRequestError(status_code, "INVALID_STRUCTURED_OUTPUT", "Model output was not valid JSON", raw_response=raw) from exc
        try:
            _validate_against_schema(structured, OPENAI_ANALYSIS_SCHEMA)
            structured, rejected_claims = reject_claims_with_unknown_evidence(structured, evidence)
            validate_openai_response(structured, evidence)
        except (TypeError, ValueError) as exc:
            raise OpenAIRequestError(status_code, "STRUCTURED_VALIDATION_FAILED", str(exc), raw_response=raw) from exc
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
    def __init__(self, status_code: int, code: str, message: str, *, raw_response: Any = None):
        # Scrub common API-key shapes in unexpected provider diagnostics.
        message = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", message)
        self.status_code, self.code, self.message = status_code, code, message[:500]
        self.raw_response = raw_response
        super().__init__(f"OpenAI request failed ({status_code}, {code}): {self.message}")


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
    """Remove individual claims with unknown evidence IDs; preserve their audit trail.

    Other response structures remain subject to strict whole-response checks.
    This permits supported claims to proceed to review without laundering a
    claim that cites evidence the application did not supply.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("Model response must be an object")
    supplied = {str(item.get("id")) for item in evidence.get("facts", [])}
    claims = raw.get("proposed_claims")
    if not isinstance(claims, list):
        raise ValueError("proposed_claims must be an array")
    accepted, rejected = [], []
    for claim in claims:
        refs = claim.get("evidence_ids", []) if isinstance(claim, Mapping) else []
        unknown = [str(ref) for ref in refs if str(ref) not in supplied]
        if unknown:
            rejected.append({"id": str(claim.get("id") or ""),
                             "text": str(claim.get("text") or ""),
                             "evidence_ids": [str(ref) for ref in refs],
                             "unknown_evidence_ids": unknown,
                             "status": "REJECTED_UNKNOWN_EVIDENCE_ID"})
        else:
            accepted.append(claim)
    filtered = dict(raw)
    filtered["proposed_claims"] = accepted
    return filtered, rejected


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
        for comparative in re.findall(r"\b(?:best|largest|smallest|leading|only|unique|first|most)\b", claim_text, re.I):
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
        rationale="Structural provenance passed, but semantic entailment is not established automatically; model wording is excluded from recipient copy unless separately grounded by application composition.",
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
    validate_openai_response(raw, evidence)
    facts = {str(item["id"]): item for item in selection.facts}
    claims = list(raw.get("proposed_claims") or [])
    audit: list[dict[str, Any]] = []
    for claim in claims:
        refs = [str(value) for value in claim.get("evidence_ids", [])]
        cited = [facts.get(ref) for ref in refs]
        excerpt = " ".join(item.get("exact_excerpt", "") for item in cited if item)
        text = str(claim.get("text") or "")
        unsupported_comparison = bool(re.search(
            r"\b(?:not\s+just|more\s+than|less\s+than|better\s+than|worse\s+than|"
            r"best|largest|smallest|only|unique|leading|most|first)\b", text, re.I))
        if unsupported_comparison and not re.search(
                r"\b(?:not\s+just|more\s+than|less\s+than|better\s+than|worse\s+than|"
                r"best|largest|smallest|only|unique|leading|most|first)\b", excerpt, re.I):
            status = "EXCLUDED_UNSUPPORTED_COMPARISON"
            reason = "The cited first-party excerpts do not establish the comparative baseline or superlative."
        else:
            status = "AUDIT_ONLY_NOT_USED_AS_APPROVED_COPY"
            reason = "Model claim text is preserved, but recipient copy is composed from validated extracted source facts."
        audit.append({"id": str(claim.get("id") or ""), "role": str(claim.get("role") or ""), "original_text": text,
                      "evidence_ids": refs, "status": status, "reason": reason,
                      "deterministic_transformation": None})

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
    if description_fact is None:
        raise ValueError("No model-cited, first-party target-business description is available")

    # Use the first model-selected service whose citation resolves to a
    # first-party product/service fact. The wording emitted below is
    # the extractor's concise statement attached to that exact source.
    service_fact = None
    for entry in raw.get("products_services", []):
        refs = [str(value) for value in entry.get("evidence_ids", [])]
        candidates = [facts.get(ref) for ref in refs]
        candidates = [item for item in candidates if item
                      and item.get("classification") == "PRODUCT_OR_SERVICE"
                      and item.get("attribution") in {"COMPANY_STATED", "PUBLISHED_STRUCTURED_DATA"}]
        for item in candidates:
            statement = str(item.get("text") or "").strip()
            if statement and len(statement) <= 260:
                service_fact = item
                break
        if service_fact:
            break
    if service_fact is None:
        raise ValueError("No concise, cited first-party service statement is available")

    company = selection.target_company
    description = str(description_fact["text"]).strip()
    strength = str(service_fact["text"]).strip()
    service_assertion = str(service_fact["text"]).strip().rstrip(".")
    if service_assertion:
        service_assertion = service_assertion[0].lower() + service_assertion[1:]
    relevance = (
        f"The site's description of {company} alongside the specific detail that {service_assertion} "
        "makes it a useful case for studying how AI-powered search connects a business identity with its offerings."
    )
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
            "business_summary": description,
            "distinctive_detail": strength,
            "research_relevance": relevance,
            "supported_observation": "",
            "support": {"business_summary": [str(description_fact["id"])],
                        "distinctive_detail": [str(service_fact["id"])],
                        "research_relevance": [str(description_fact["id"]), str(service_fact["id"])]},
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
            "interpretation": "Uncertain model prose is excluded; recipient claims use source-derived statements.",
        },
        "claim_support": {"business_description": [str(description_fact["id"])],
                          "company_strength": [str(service_fact["id"]) ]},
        "evidence_profile": {"pages": [dict(page) for page in selection.pages],
                             "facts": profile_facts, "relationships": []},
        "evidence": {"sampled_urls": all_urls, "limitations": list(selection.limitations)},
    }
    for item in audit:
        if item["role"] == "BUSINESS_DESCRIPTION":
            item["status"] = "REPLACED_WITH_DIRECT_SOURCE_DESCRIPTION"
            item["reason"] = "The final description uses the model-cited source statement to avoid adding paraphrase detail."
            item["deterministic_transformation"] = {"evidence_id": str(description_fact["id"]),
                                                    "text": description}
        elif item["role"] == "RESEARCH_RELEVANCE":
            item["status"] = "RECOMPOSED_FROM_CITED_SOURCE_FACTS"
            item["reason"] = "Recipient relevance is composed from the cited page relationship, not copied from analyst prose."
            item["deterministic_transformation"] = {"evidence_ids": [str(description_fact["id"]),
                                                                          str(service_fact["id"])],
                                                    "text": relevance}
    return personalization, audit


