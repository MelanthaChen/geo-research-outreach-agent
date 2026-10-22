"""Explicit, configurable OpenAI-compatible preliminary screening adapter.

The adapter is transport-only and fail-closed.  It is never selected unless a
provider URL, model, and explicit approval flag are configured.  It does not
implement GEO Engine audits or experiments.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


PROMPT_VERSION = "preliminary-geo-screen/v2"


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key: str
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    timeout_seconds: float = 45.0
    max_output_tokens: int = 1200

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def configured_provider() -> ProviderConfig | None:
    base = os.getenv("GEO_SCREENING_OPENAI_BASE_URL", "").strip()
    model = os.getenv("GEO_SCREENING_MODEL", "").strip()
    key = os.getenv("GEO_SCREENING_API_KEY", "").strip()
    if not base or not model or not key:
        return None
    if os.getenv("GEO_SCREENING_APPROVED", "").casefold() != "true":
        return None
    return ProviderConfig(
        base_url=base,
        model=model,
        api_key=key,
        input_cost_per_million=float(os.getenv("GEO_SCREENING_INPUT_USD_PER_MILLION", "0")),
        output_cost_per_million=float(os.getenv("GEO_SCREENING_OUTPUT_USD_PER_MILLION", "0")),
    )


def estimate_cost(candidate_count: int, *, input_tokens_per_candidate: int = 2500, output_tokens_per_candidate: int = 900, config: ProviderConfig | None = None) -> dict[str, Any]:
    config = config or ProviderConfig("", "", "")
    input_tokens = candidate_count * input_tokens_per_candidate
    output_tokens = candidate_count * output_tokens_per_candidate
    cost = input_tokens / 1_000_000 * config.input_cost_per_million + output_tokens / 1_000_000 * config.output_cost_per_million
    return {"candidates": candidate_count, "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost_usd": round(cost, 6), "pricing_configured": bool(config.input_cost_per_million or config.output_cost_per_million)}


def build_prompt(*, name: str, industry: str | None, location: str | None, pages: list[dict[str, str]]) -> str:
    evidence = "\n\n".join(f"URL: {p.get('url','')}\nTEXT: {p.get('text','')[:12000]}" for p in pages)
    return f"""Evaluate this website for a lightweight preliminary GEO research invitation.\nBusiness: {name}\nIndustry: {industry or 'unknown'}\nLocation: {location or 'unknown'}\n\nReturn JSON only with keys: rating (HIGH, MEDIUM, LOW, INSUFFICIENT_EVIDENCE), entity_clarity, discoverability, answerability, geographic_relevance, factual_content, citation_worthy_content, structured_information, problems (array), opportunities (array), realistic_customer_queries (array), suggested_improvements (array), supporting_evidence (array of URL/excerpt objects), confidence, limitations. Do not claim AI rankings, absence, uplift, citations, or measured improvements.\n\nObserved public pages:\n{evidence}"""


def call(config: ProviderConfig, *, name: str, industry: str | None, location: str | None, pages: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, int]]:
    body = {"model": config.model, "messages": [{"role": "system", "content": "You are a careful research screening assistant. Use only supplied website evidence."}, {"role": "user", "content": build_prompt(name=name, industry=industry, location=location, pages=pages)}], "temperature": 0, "max_tokens": config.max_output_tokens, "response_format": {"type": "json_object"}}
    request = urllib.request.Request(config.chat_url, data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"AI_PROVIDER_REQUEST_FAILED:{type(exc).__name__}") from exc
    content = payload["choices"][0]["message"]["content"]
    result = json.loads(content)
    usage = payload.get("usage") or {}
    return result, {"input_tokens": int(usage.get("prompt_tokens", 0)), "output_tokens": int(usage.get("completion_tokens", 0))}

