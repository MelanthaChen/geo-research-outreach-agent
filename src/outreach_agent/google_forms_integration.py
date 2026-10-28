"""Google Forms/Sheets boundary for the existing outreach lifecycle.

The adapter is deliberately transport-neutral: tests can pass rows directly,
while production synchronization must provide an authenticated Sheets reader.
It never treats a form reference or self-reported email as verified consent.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

FORM_ID = "1JaUn431g9KzjmSBool8WzvCK_bjQ-EeTVteJa4oU4_4"
RESPONSE_SPREADSHEET_ID = "1CqJ3abpAci7hBbGYULNpyvkjkmjn6E8uyOoSrUMOHps"
FORM_VIEW_URL = "https://docs.google.com/forms/d/e/1FAIpQLSe_a-Dg-qahIGMNfnjH8x3kYfn_R_e9lFaO4_fQPBIPIfLq-w/viewform"
INVITATION_REFERENCE_ENTRY = "entry.1773870597"
FORM_ENTRY_VERSION = "google-form-entry/v1"


def make_invitation_reference(profile_id: str, *, nonce: str) -> str:
    """Create a non-secret, unique correlation reference for one invitation."""
    digest = hashlib.sha256(f"{profile_id}:{nonce}".encode()).hexdigest()[:16].upper()
    return f"GEO-{digest}"


def prefilled_form_url(invitation_reference: str, *, form_url: str | None = None, entry_id: str | None = None) -> str:
    target = (form_url or os.environ.get("GEO_RESEARCH_FORM_URL") or FORM_VIEW_URL).rstrip("?")
    field = entry_id or os.environ.get("GEO_RESEARCH_FORM_ENTRY_ID") or INVITATION_REFERENCE_ENTRY
    if not field.startswith("entry."):
        raise ValueError("Google Forms entry ID must start with entry.")
    return f"{target}?{urlencode({'usp': 'pp_url', field: invitation_reference})}"


@dataclass(frozen=True)
class SheetResponse:
    invitation_reference: str
    research_interest: str
    research_authorization: str
    representative_name: str
    representative_role: str
    contact_email: str
    submitted_at: str
    raw: dict[str, Any]

    # Compatibility aliases for the original four-question adapter.
    @property
    def respondent_name(self) -> str:
        return self.representative_name

    @property
    def consent(self) -> str:
        return self.research_interest


def parse_sheet_row(
    row: dict[str, Any],
    *,
    reference_header: str = "Invitation Reference",
    interest_header: str = "Is your company interested in participating in this research?",
    authorization_header: str = "Does your company authorize our research team to conduct the research described above?",
    name_header: str = "Full Name of the Authorized Company Representative",
    role_header: str = "Job Title of the Authorized Company Representative",
    email_header: str = "Business Email Address of the Authorized Company Representative",
    # Legacy headers remain accepted for already-created response exports.
    legacy_name_header: str = "Your Name and Role",
    legacy_email_header: str = "Business Email Address",
    legacy_consent_header: str = "Research Participation & Consent",
) -> SheetResponse:
    def value(header: str) -> str:
        return str(row.get(header, "") or "").strip()

    interest = value(interest_header).casefold()
    authorization = value(authorization_header).casefold()
    representative_name = value(name_header)
    # The existing response sheet predates the current form question title.
    # Treat its equivalent field as the same canonical job-title value.
    representative_role = value(role_header) or value("Your Role / Position")
    # The current Form already has this exact question; retain the former
    # short header as an export compatibility alias.
    email = value(email_header) or value("Business Email Address")
    if not any(key in row for key in (interest_header, authorization_header, name_header, role_header, email_header)):
        legacy_consent = value(legacy_consent_header).casefold()
        interest = legacy_consent if legacy_consent in {"yes", "no"} else "needs_review"
        authorization = ""
        legacy_identity = value(legacy_name_header)
        if " / " in legacy_identity:
            representative_name, representative_role = [part.strip() for part in legacy_identity.split(" / ", 1)]
        else:
            representative_name = legacy_identity
        email = value(legacy_email_header)
    if interest not in {"yes", "no"}:
        interest = "needs_review"
    if authorization not in {"yes", "no"}:
        authorization = "needs_review"
    return SheetResponse(value(reference_header), interest.upper(), authorization.upper(), representative_name, representative_role, email, value("Timestamp"), dict(row))


def sync_sheet_rows(rows: Iterable[dict[str, Any]], *, process: Callable[[SheetResponse], str]) -> dict[str, int]:
    """Process authenticated rows through the lifecycle's idempotent handler."""
    counts = {"seen": 0, "processed": 0, "duplicates": 0, "needs_review": 0}
    for row in rows:
        counts["seen"] += 1
        status = process(parse_sheet_row(row))
        if status == "DUPLICATE":
            counts["duplicates"] += 1
        elif status == "NEEDS_REVIEW":
            counts["needs_review"] += 1
        else:
            counts["processed"] += 1
    return counts


def authenticated_sheets_reader(*, spreadsheet_id: str, range_name: str, service) -> list[dict[str, Any]]:
    """Read a response range with an injected authenticated Google API service.

    Credential acquisition is intentionally outside this adapter. A caller must
    provide a service created with OAuth/service-account credentials and the
    Sheets read scope; no unauthenticated fallback is permitted.
    """
    if service is None:
        raise RuntimeError("authenticated Google Sheets service is required")
    payload = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    values = payload.get("values") or []
    if not values:
        return []
    headers = [str(item) for item in values[0]]
    return [dict(zip(headers, row)) for row in values[1:]]
