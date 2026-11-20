"""Evidence-based business-email readiness shared by screening and invitations."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit
from typing import Any


BUSINESS_EMAIL_TYPES = frozenset({
    "GENERAL_BUSINESS_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL",
    "PARTNERSHIP_EMAIL", "SALES_MARKETING_EMAIL", "NAMED_PERSON_EMAIL",
})
VERIFIED_REVIEW_STATES = frozenset({"VERIFIED", "CONTACT_VERIFIED", "APPROVED"})
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RESTRICTED_PURPOSE = re.compile(
    r"\b(?:support|help\s*desk|technical\s*support|privacy|legal|abuse|security|dpo|hr|"
    r"accessibility|billing|careers?|recruit(?:ing|ment)?|jobs?|customer\s+service)\b", re.I
)


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
    website = str(_value(record, "official_website") or "")
    channels_raw = _value(record, "contact_channels_json", _value(record, "contact_channels", []))
    channels = _json_list(channels_raw)
    result = []
    for item in channels:
        email = str(item.get("email") or "").strip().casefold()
        kind = str(item.get("channel_type") or "").upper()
        source = str(item.get("source_url") or item.get("channel_url") or "").strip()
        evidence = str(item.get("evidence") or item.get("evidence_text") or item.get("excerpt") or "")
        purpose = " ".join(str(item.get(key) or "") for key in ("purpose", "evidence", "evidence_text", "channel_type"))
        review_status = str(item.get("review_status") or "").upper()
        if not EMAIL_RE.fullmatch(email) or kind not in BUSINESS_EMAIL_TYPES:
            continue
        if not source or not same_first_party_site(website, source):
            continue
        if review_status == "REJECTED":
            continue
        if email not in evidence.casefold():
            continue
        if RESTRICTED_PURPOSE.search(email.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ") + " " + purpose):
            continue
        result.append({**item, "email": email, "source_url": source, "evidence": evidence})
    return result


def verified_business_email_channels(record: Any) -> list[dict[str, Any]]:
    """Return only reviewed, purpose-appropriate, evidence-matched business emails."""
    if str(_value(record, "contact_verification_status", "")).upper() != "CONTACT_VERIFIED":
        return []
    verified = []
    for item in first_party_business_email_evidence(record):
        if str(item.get("review_status") or "").upper() not in VERIFIED_REVIEW_STATES:
            continue
        explicit_states = [item[key] for key in ("verification_status", "email_verification_status") if item.get(key)]
        if any(str(status).upper() not in VERIFIED_REVIEW_STATES for status in explicit_states):
            continue
        verified.append(item)
    return verified


def contact_readiness_counts(records: list[Any], recovered_count: int = 0) -> dict[str, int]:
    verified = missing = pending = 0
    for record in records:
        if verified_business_email_channels(record):
            verified += 1
        elif first_party_business_email_evidence(record):
            pending += 1
        else:
            missing += 1
    return {"contact_verified": verified, "contact_missing": missing,
            "contact_pending_verification": pending, "recovered": int(recovered_count)}


def evidence_fingerprint(profile_id: str, item: dict[str, Any]) -> str:
    payload = "\x1f".join((profile_id, str(item.get("email") or ""), str(item.get("source_url") or ""), str(item.get("evidence") or "")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
