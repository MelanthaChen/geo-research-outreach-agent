"""Provider-free outreach preparation primitives and an offline-only demo transport."""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from string import Formatter
from typing import Any


class OutreachState(StrEnum):
    NOT_PREPARED = "NOT_PREPARED"
    DRAFT_READY = "DRAFT_READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    INTERESTED = "INTERESTED"
    DECLINED = "DECLINED"
    OPTED_OUT = "OPTED_OUT"
    HANDOFF_READY = "HANDOFF_READY"


ALLOWED_TRANSITIONS: dict[OutreachState, set[OutreachState]] = {
    OutreachState.NOT_PREPARED: {OutreachState.DRAFT_READY},
    OutreachState.DRAFT_READY: {OutreachState.NEEDS_REVIEW},
    OutreachState.NEEDS_REVIEW: {OutreachState.APPROVED},
    OutreachState.APPROVED: {OutreachState.SENDING},
    OutreachState.SENDING: {OutreachState.SENT, OutreachState.FAILED},
    OutreachState.FAILED: {OutreachState.APPROVED},
    OutreachState.SENT: {OutreachState.RESPONSE_RECEIVED},
    OutreachState.RESPONSE_RECEIVED: {OutreachState.INTERESTED, OutreachState.DECLINED, OutreachState.OPTED_OUT},
    OutreachState.INTERESTED: {OutreachState.HANDOFF_READY},
    OutreachState.DECLINED: set(),
    OutreachState.OPTED_OUT: set(),
    OutreachState.HANDOFF_READY: set(),
}

SUITABLE_EMAIL_TYPES = {
    "PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL",
    "NAMED_PERSON_EMAIL", "SALES_MARKETING_EMAIL",
}
SUITABLE_CHANNEL_TYPES = SUITABLE_EMAIL_TYPES | {"CONTACT_FORM"}
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)


def validate_recipient(value: str | None) -> str:
    recipient = (value or "").strip()
    if not EMAIL_RE.fullmatch(recipient) or len(recipient) > 320:
        raise ValueError("recipient must be one syntactically valid email address")
    return recipient.casefold()


def render_template(template: str, values: dict[str, str]) -> str:
    required = {name for _, name, _, _ in Formatter().parse(template) if name}
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError("missing template variables: " + ", ".join(missing))
    try:
        return template.format_map(values).strip()
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError(f"could not render outreach template: {exc}") from exc


def idempotency_key(company_id: int, contact_id: int, recipient: str, subject: str, body: str) -> str:
    source = "\0".join((str(company_id), str(contact_id), validate_recipient(recipient), subject.strip(), body.strip()))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def transition(current: OutreachState, target: OutreachState) -> OutreachState:
    if current == target:
        return current
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid outreach state transition: {current.value} -> {target.value}")
    return target


@dataclass(frozen=True)
class ApprovalGate:
    identity_verified: bool
    eligibility: str
    channel_type: str
    recipient_valid: bool
    company_review_status: str
    channel_review_status: str
    real_outreach_approval: bool = False
    message_approved: bool = False
    sender_configured: bool = False
    live_sending_enabled: bool = False
    opted_out: bool = False


def evaluate_delivery_gate(gate: ApprovalGate, *, demo_fixture_approval: bool = False) -> str:
    if gate.opted_out:
        return "SUPPRESSED_OPT_OUT"
    if not gate.identity_verified:
        return "BLOCKED_IDENTITY_UNVERIFIED"
    if gate.eligibility != "ELIGIBLE":
        return "BLOCKED_ELIGIBILITY_NOT_APPROVED"
    if gate.channel_type not in SUITABLE_CHANNEL_TYPES or not gate.recipient_valid:
        return "BLOCKED_UNSUITABLE_CHANNEL"
    if demo_fixture_approval:
        return "SIMULATED_APPROVAL_ONLY"
    if gate.company_review_status != "APPROVED":
        return "BLOCKED_COMPANY_REVIEW"
    if gate.channel_review_status != "APPROVED":
        return "BLOCKED_CHANNEL_REVIEW"
    if not gate.real_outreach_approval or not gate.message_approved:
        return "BLOCKED_OUTREACH_OR_MESSAGE_APPROVAL"
    if not gate.sender_configured:
        return "BLOCKED_SENDER_NOT_CONFIGURED"
    if not gate.live_sending_enabled:
        return "BLOCKED_LIVE_SENDING_DISABLED"
    # No live transport exists in Phase 5A, so all non-demo delivery remains blocked.
    return "BLOCKED_NO_LIVE_PROVIDER_IN_PHASE_5A"


class SuppressionRegistry:
    """CSV-backed opt-out list for future integrations; demo responses never persist here."""

    FIELDS = ["email", "reason", "source"]

    def __init__(self, path: Path, suppressed: dict[str, tuple[str, str]] | None = None):
        self.path = path
        self._suppressed = suppressed or {}

    @classmethod
    def load(cls, path: Path) -> "SuppressionRegistry":
        values: dict[str, tuple[str, str]] = {}
        if path.exists():
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if row.get("email"):
                        values[validate_recipient(row["email"])] = (row.get("reason", ""), row.get("source", ""))
        return cls(path, values)

    def is_suppressed(self, email: str) -> bool:
        return validate_recipient(email) in self._suppressed

    def suppress(self, email: str, *, reason: str, source: str, simulated: bool = False) -> None:
        recipient = validate_recipient(email)
        self._suppressed[recipient] = (reason, source)
        if not simulated:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.FIELDS)
            writer.writeheader()
            for email, (reason, source) in sorted(self._suppressed.items()):
                writer.writerow({"email": email, "reason": reason, "source": source})
            handle.flush()
        temporary.replace(self.path)


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    idempotency_key: str
    provider_message_id: str
    error: str = ""


class DemoTransport:
    """Never performs I/O. This is intentionally the only transport in Phase 5A."""

    def __init__(self, successful: dict[str, DeliveryResult] | None = None):
        self.successful = dict(successful or {})

    def deliver(self, *, company_id: int, contact_id: int, recipient: str, subject: str,
                body: str, approval_status: str, simulate_failure: bool = False) -> DeliveryResult:
        key = idempotency_key(company_id, contact_id, recipient, subject, body)
        message_id = "SIMULATED-" + key[:20]
        if approval_status != "SIMULATED_APPROVAL_ONLY":
            return DeliveryResult("BLOCKED", key, "", "demo approval fixture required")
        if key in self.successful:
            previous = self.successful[key]
            return DeliveryResult("SIMULATED_DUPLICATE_SUPPRESSED", key, previous.provider_message_id)
        if simulate_failure:
            return DeliveryResult("SIMULATED_FAILED", key, "", "fixture delivery failure; no network request made")
        result = DeliveryResult("SIMULATED_DELIVERED", key, message_id)
        self.successful[key] = result
        return result


def classify_response(response_type: str) -> OutreachState:
    normalized = response_type.strip().upper()
    mapping = {
        "INTERESTED": OutreachState.INTERESTED,
        "DECLINED": OutreachState.DECLINED,
        "OPTED_OUT": OutreachState.OPTED_OUT,
        "OTHER": OutreachState.RESPONSE_RECEIVED,
    }
    if normalized not in mapping:
        raise ValueError(f"unsupported response classification: {response_type}")
    return mapping[normalized]


def prepare_signup_handoff(*, current_state: OutreachState, signup_url: str,
                           signup_url_approval_fixture: bool, demo: bool = False) -> dict[str, str]:
    if current_state != OutreachState.INTERESTED:
        return {"status": "BLOCKED_NO_EXPLICIT_INTEREST", "url": "", "consent_status": "NOT_ESTABLISHED"}
    if not signup_url or not signup_url_approval_fixture:
        return {"status": "NEEDS_CONFIGURED_AND_APPROVED_SIGNUP_URL", "url": "", "consent_status": "NOT_ESTABLISHED"}
    if demo:
        return {"status": "SIMULATED_HANDOFF_READY", "url": signup_url, "consent_status": "NOT_ESTABLISHED"}
    return {"status": "HANDOFF_READY", "url": signup_url, "consent_status": "NOT_ESTABLISHED"}


def map_contact_form_fields(evidence: str) -> dict[str, str]:
    """Map only field types visible in saved form evidence; never submits a form."""
    text = evidence.casefold()
    mapping: dict[str, str] = {}
    patterns = {
        "name": (r"\bname\b|first_name|full_name", "Visible name field"),
        "email": (r"\bemail\b", "Visible email field"),
        "phone": (r"\bphone\b|telephone", "Visible phone field"),
        "company": (r"\bcompany\b|organization", "Visible company field"),
        "subject": (r"\bsubject\b", "Visible subject field"),
        "message": (r"\bmessage\b|comments?|how can we help", "Visible message field"),
    }
    for key, (pattern, label) in patterns.items():
        if re.search(pattern, text):
            mapping[key] = label
    return mapping


def form_submission_safety(evidence: str) -> dict[str, str]:
    text = evidence.casefold()
    captcha = bool(re.search(r"captcha|recaptcha|hcaptcha", text))
    consent = bool(re.search(r"consent|privacy policy|terms and conditions|i agree", text))
    return {
        "captcha_detected": str(captcha).lower(),
        "consent_detected": str(consent).lower(),
        "submission_status": "NOT_SUBMITTED_DEMO",
        "manual_confirmation_required": "true",
    }
