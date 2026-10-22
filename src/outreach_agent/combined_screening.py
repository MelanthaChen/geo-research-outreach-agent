"""One-pass preliminary GEO assessment plus invitation drafting."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from outreach_agent.preliminary_screening import ScreeningConfig, _fetch, _hash
from outreach_agent.outreach_pipeline import mail_merge_safe_subject
from outreach_agent.research_lifecycle import REGISTRY_DB, _connect, get_record, now, persist_combined_screening_and_draft
from outreach_agent.screening_provider import PROMPT_VERSION, call as call_ai, configured_provider

MODES = {"RULE_BASED", "LOCAL_LLM", "PAID_API"}
RATINGS = {"HIGH", "MEDIUM", "LOW", "INSUFFICIENT_EVIDENCE"}


def _cached_pages(profile_id: str, path: Path) -> tuple[list[dict[str, str]], dict[str, Any]] | None:
    with _connect(path) as db:
        row = db.execute("SELECT result_json FROM screening_analysis_results WHERE profile_id=? ORDER BY created_at DESC LIMIT 1", (profile_id,)).fetchone()
    if not row:
        return None
    try:
        result = json.loads(row["result_json"])
    except (TypeError, ValueError):
        return None
    pages = result.get("pages")
    return (pages, result) if isinstance(pages, list) and pages else None


def _rule_based(*, name: str, industry: str | None, location: str | None, pages: list[dict[str, str]]) -> dict[str, Any]:
    text = " ".join(str(page.get("text") or "") for page in pages)
    lower = text.casefold()
    signals = {
        "entity_clarity": bool(name.casefold() in lower or any(token in lower for token in name.casefold().split() if len(token) > 3)),
        "service_discoverability": bool(re.search(r"\b(service|services|products?|menu|solutions?|offer|book|reserve|shop)\b", lower)),
        "question_answerability": bool(re.search(r"\b(faq|frequently asked|hours|pricing|price|location|contact|how|what|when)\b", lower)),
        "geographic_relevance": bool(location and any(token.casefold() in lower for token in str(location).split() if len(token) > 2)),
        "factual_content": len(text) >= 900,
        "structured_information": bool(re.search(r"\b(address|phone|email|hours|located|established)\b", lower)),
    }
    score = sum(bool(value) for value in signals.values())
    rating = "HIGH" if score >= 5 else "MEDIUM" if score >= 3 else "LOW" if score >= 1 else "INSUFFICIENT_EVIDENCE"
    first = pages[0] if pages else {"url": "", "text": ""}
    excerpt = str(first.get("text") or "")[:500]
    opportunities = []
    if not signals["entity_clarity"]: opportunities.append({"title": "Clarify the organization and location", "problem": "The inspected text does not clearly connect the organization name to its location.", "suggestion": "Add a concise organization, location, and service summary near the top of the site."})
    if not signals["service_discoverability"]: opportunities.append({"title": "Make products or services easier to identify", "problem": "The inspected text has limited explicit product or service language.", "suggestion": "Use descriptive service headings and plain-language summaries."})
    if not signals["question_answerability"]: opportunities.append({"title": "Answer common customer questions", "problem": "No clear FAQ or customer-question section was observed.", "suggestion": "Add factual answers for common questions such as services, hours, location, and booking."})
    if not opportunities: opportunities.append({"title": "Strengthen factual answer paths", "problem": "The site contains useful information but can make key facts easier to retrieve.", "suggestion": "Organize the strongest factual details into concise, linked sections."})
    opportunities = opportunities[:3]
    queries = [f"What does {name} offer in {location or 'the local area'}?", f"How can I contact or visit {name}?", f"What should customers know before choosing {name}?"]
    talking_points = [f"{item['title']}: {item['suggestion']}" for item in opportunities[:2]]
    return {"rating": rating, "signals": signals, "opportunities": opportunities, "talking_points": talking_points, "realistic_customer_queries": queries, "confidence": "MEDIUM" if score >= 3 else "LOW", "limitations": ["Rule-based preliminary assessment; no LLM visibility test or measured improvement was performed."], "supporting_evidence": [{"url": first.get("url", ""), "excerpt": excerpt}]}


def _invitation(*, name: str, industry: str | None, location: str | None, screening: dict[str, Any]) -> dict[str, str]:
    points = screening.get("talking_points") or [str(item.get("title")) for item in (screening.get("opportunities") or [])[:2]]
    observations = " ".join(str(item) for item in points[:2])
    subject = mail_merge_safe_subject(f"Research invitation for {name}: improving how website information is found")
    body = (f"Hello,\n\nWe are inviting {name} to participate in a research study about how clear, factual website information can support answers to realistic customer questions.\n\n"
            f"In a preliminary review of your public website, we observed: {observations}. These are evidence-based research opportunities, not a claim about current AI rankings or a guarantee of improvement.\n\n"
            f"The study would consider public information about your {industry or 'business'} in {location or 'the United States'} and would require your explicit consent before any deeper GEO research or experiments.\n\n"
            "Please review the company-specific questionnaire here: {{questionnaire_url}}\n\n"
            "Participation is voluntary. No website change or experiment will be made without separate approval.\n\nThank you.\n")
    return {"subject": subject, "body": body}


def validate_analysis_result(result: dict[str, Any]) -> None:
    """Validate the persisted one-pass contract before it reaches the registry."""
    required = {"mode", "content_hash", "pages", "evidence", "screening", "invitation"}
    missing = sorted(required - set(result))
    if missing:
        raise ValueError(f"combined analysis missing fields: {', '.join(missing)}")
    if result["mode"] not in MODES:
        raise ValueError("combined analysis has unsupported mode")
    screening = result["screening"]
    if not isinstance(screening, dict) or screening.get("rating") not in RATINGS:
        raise ValueError("combined analysis has unsupported GEO rating")
    if not isinstance(result["evidence"], list) or not result["evidence"]:
        raise ValueError("combined analysis requires website evidence")
    if any(not item.get("url") or not item.get("excerpt") for item in result["evidence"]):
        raise ValueError("each evidence item requires a source URL and excerpt")
    invitation = result["invitation"]
    if not isinstance(invitation, dict) or not invitation.get("subject") or not invitation.get("body"):
        raise ValueError("combined analysis requires a complete invitation draft")
    prohibited = re.compile(r"\b(we\s+guarantee|guaranteed\s+(increase|uplift|improvement)|\d+\s*%\s*(increase|uplift|improvement))\b", re.I)
    if prohibited.search(str(invitation["body"])):
        raise ValueError("invitation contains an unsupported guarantee or fabricated metric")


def run_combined_analysis(profile_id: str, *, mode: str = "RULE_BASED", recipient: str, secret: str, path: Path = REGISTRY_DB, base_url: str | None = None, config: ScreeningConfig | None = None, force_refetch: bool = False) -> dict[str, Any]:
    mode = mode.upper()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    record = get_record(profile_id, path)
    cached = None if force_refetch else _cached_pages(profile_id, path)
    if cached:
        pages, prior = cached
        fetch_meta = {"mode": "CACHED", "source_analysis_id": prior.get("analysis_id")}
    else:
        pages, fetch_meta = _fetch(record["official_website"], config or ScreeningConfig(), mock=False, name=record["name"], industry=record.get("industry"))
    input_tokens = output_tokens = 0
    provider = mode
    model = None
    if mode == "RULE_BASED":
        screening = _rule_based(name=record["name"], industry=record.get("industry"), location=record.get("location"), pages=pages)
    else:
        provider_config = configured_provider()
        if provider_config is None:
            raise RuntimeError(f"{mode}_PROVIDER_NOT_CONFIGURED_OR_APPROVED")
        if mode == "LOCAL_LLM" and provider_config.base_url.startswith(("https://api.openai.com", "https://api.anthropic.com")):
            raise PermissionError("LOCAL_LLM mode cannot use a paid hosted endpoint")
        raw, usage = call_ai(provider_config, name=record["name"], industry=record.get("industry"), location=record.get("location"), pages=pages)
        screening = raw
        input_tokens, output_tokens = usage["input_tokens"], usage["output_tokens"]
        provider = "OPENAI_COMPATIBLE_" + mode
        model = provider_config.model
    evidence = [{"url": page.get("url", ""), "excerpt": str(page.get("text") or "")[:800], "captured_at": now(), "content_hash": _hash(str(page.get("text") or "")), "fetch": fetch_meta} for page in pages]
    content_hash = _hash(json.dumps([{"url": p.get("url"), "content_hash": _hash(str(p.get("text") or ""))} for p in pages], sort_keys=True))
    result = {"mode": mode, "provider": provider, "model": model, "prompt_version": PROMPT_VERSION if mode != "RULE_BASED" else "rule-based-preliminary/v1", "content_hash": content_hash, "pages": pages, "evidence": evidence, "screening": screening, "invitation": _invitation(name=record["name"], industry=record.get("industry"), location=record.get("location"), screening=screening), "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost_usd": 0}
    validate_analysis_result(result)
    result.update(persist_combined_screening_and_draft(profile_id, result=result, recipient=recipient, secret=secret, path=path, base_url=base_url))
    return result

