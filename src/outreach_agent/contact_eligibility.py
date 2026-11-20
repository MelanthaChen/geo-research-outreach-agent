"""Evidence-based business-email readiness shared by screening and invitations."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit
from typing import Any

from outreach_v2.contact_purpose import GENERAL_LABEL, classify_public_contact_purpose

BUSINESS_EMAIL_TYPES = frozenset({
    "GENERAL_BUSINESS_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL",
    "PARTNERSHIP_EMAIL", "SALES_MARKETING_EMAIL", "NAMED_PERSON_EMAIL",
})
BLOCKED_CONTACT_STATES = frozenset({"INVALID", "REJECTED", "SUPPRESSED", "BLOCKED", "UNSUBSCRIBED",
    "MAYBE", "DO_NOT_USE", "UNSUITABLE"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except (TypeError, ValueError):
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _value(record: Any, key: str, default: Any = None) -> Any:
    try:
        value = record.get(key, default)
    except AttributeError:
        try:
            value = record[key]
        except (KeyError, IndexError, TypeError):
            value = default
    return default if value is None else value


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def same_first_party_site(website: str, source_url: str) -> bool:
    website_host, source_host = _host(website), _host(source_url)
    return bool(website_host and source_host and (source_host == website_host or source_host.endswith("." + website_host)))


def first_party_business_email_evidence(record: Any) -> list[dict[str, Any]]:
    """Return exact email claims grounded in evidence from the verified website.

    This checks evidence quality, not approval state. A channel can be recovered
    only when the official identity is already verified and the source excerpt
    contains that exact mailbox.
    """
    if str(_value(record, "identity_verification_status", "")).upper() != "VERIFIED":
        return []
    if str(_value(record, "suppression_status", "")).upper() in BLOCKED_CONTACT_STATES:
        return []
    if str(_value(record, "contact_verification_status", "")).upper() in BLOCKED_CONTACT_STATES:
        return []
    website = str(_value(record, "official_website") or "")
    channels_raw = _value(record, "contact_channels_json", _value(record, "contact_channels", []))
    channels = _json_list(channels_raw)
    result = []
    for item in channels:
        email = str(item.get("email") or "").strip().casefold()
        kind = str(item.get("channel_type") or "").upper()
        source = str(item.get("source_url") or item.get("channel_url") or "").strip()
        evidence = str(item.get("evidence") or item.get("evidence_text") or item.get("excerpt") or "")
        purpose = str(item.get("purpose") or item.get("purpose_classification") or "")
        review_status = str(item.get("review_status") or "").upper()
        if not EMAIL_RE.fullmatch(email) or kind not in BUSINESS_EMAIL_TYPES:
            continue
        if not source or not same_first_party_site(website, source):
            continue
        states = [review_status]
        states.extend(str(item.get(key) or "").upper() for key in (
            "verification_status", "email_verification_status", "validation_status",
            "contact_status", "suppression_status", "status"))
        if any(state in BLOCKED_CONTACT_STATES for state in states):
            continue
        if item.get("suppressed") is True or item.get("is_suppressed") is True:
            continue
        if not any(match.group(0).casefold() == email for match in
                   re.finditer(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", evidence, re.I)):
            continue
        if classify_public_contact_purpose(email, evidence, purpose, source) != GENERAL_LABEL:
            continue
        result.append({**item, "email": email, "source_url": source, "evidence": evidence})
    return result


def eligible_business_email_channels(record: Any) -> list[dict[str, Any]]:
    """Evidence-backed public emails; contact review freshness is not a gate."""
    return first_party_business_email_evidence(record)


def contact_readiness_counts(records: list[Any]) -> dict[str, int]:
    eligible = sum(bool(eligible_business_email_channels(record)) for record in records)
    return {"eligible_for_invitation_drafting": eligible,
            "no_eligible_contact": max(0, len(records) - eligible)}


def evidence_fingerprint(profile_id: str, item: dict[str, Any]) -> str:
    payload = "\x1f".join((profile_id, str(item.get("email") or ""), str(item.get("source_url") or ""), str(item.get("evidence") or "")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
