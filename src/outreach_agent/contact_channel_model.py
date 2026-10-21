"""Company-level channel coverage classification for review and campaign routing."""

from __future__ import annotations

EMAIL_CHANNELS = {
    "GENERAL_BUSINESS_EMAIL",
    "BUSINESS_DEVELOPMENT_EMAIL",
    "SALES_MARKETING_EMAIL",
    "PARTNERSHIP_EMAIL",
    "NAMED_PERSON_EMAIL",
}


def classify_contact_channels(channels: list[dict]) -> str:
    """Classify evidenced first-party channels without implying human approval."""
    suitable = [row for row in channels if row.get("is_suitable_first_party")]
    has_email = any(row.get("channel_type") in EMAIL_CHANNELS and row.get("email") for row in suitable)
    has_form = any(row.get("channel_type") == "CONTACT_FORM" and row.get("channel_url") for row in suitable)
    if has_email and has_form:
        return "EMAIL_AND_FORM"
    if has_email:
        return "EMAIL_ONLY"
    if has_form:
        return "FORM_ONLY"
    unresolved = any(
        str(row.get("review_status") or "PENDING").upper() != "REJECTED"
        and (row.get("email") or row.get("channel_url") or row.get("channel_type"))
        for row in channels
    )
    return "NEEDS_REVIEW" if unresolved else "NO_CONTACT_CHANNEL"
