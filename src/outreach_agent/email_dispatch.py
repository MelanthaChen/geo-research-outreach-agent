"""Provider-neutral, approval-gated outreach dispatch.

This module owns batching, safety policy, retries, rate limiting, and the
durable dispatch ledger. Providers only authenticate and submit one message.
No provider is contacted by importing this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
import secrets
import base64
import hashlib

from outreach_agent.research_lifecycle import REGISTRY_DB, _connect, _hash, init_registry, now, approve_email_batch


class EmailProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderResult:
    message_id: str


class EmailProvider(Protocol):
    name: str
    enabled: bool

    def send(self, *, sender: str, recipient: str, subject: str, body: str) -> ProviderResult: ...


@dataclass(frozen=True)
class DispatchConfig:
    max_batch_size: int = 50
    max_per_minute: int = 30
    max_attempts: int = 3
    allowed_recipients: frozenset[str] | None = None
    retry_backoff: Callable[[int], None] | None = None


@dataclass(frozen=True)
class DispatchOutcome:
    draft_id: str
    status: str
    event_id: str | None = None
    message_id: str | None = None
    error: str | None = None
    attempts: int = 0


class GmailProviderAdapter:
    """Adapter over the existing Gmail sender; no Gmail behavior is duplicated."""
    name = "gmail_api"

    def __init__(self, *, settings, enabled: bool = True):
        self.settings = settings
        self.enabled = enabled

    def send(self, *, sender: str, recipient: str, subject: str, body: str) -> ProviderResult:
        from outreach_agent.gmail_integration import send_gmail_message
        try:
            result = send_gmail_message(recipient=recipient, sender=sender, subject=subject, body=body, settings=self.settings)
        except Exception as exc:
            raise EmailProviderError(str(exc), retryable=False) from exc
        return ProviderResult(message_id=str(result.get("provider_message_id") or result.get("message_id") or result.get("id") or ""))

    def send_document(self, *, sender, recipient, subject, body, filename, content):
        from outreach_agent.gmail_integration import send_gmail_message
        result = send_gmail_message(sender=sender, recipient=recipient, subject=subject, body=body,
                                    settings=self.settings, attachment=(filename, content))
        return ProviderResult(result["provider_message_id"])


def configured_dispatch():
    """Use the existing sender identity and credentials; never silently simulate."""
    import os
    from outreach_agent.email_test_settings import PREFERRED_SENDER, AUTHORIZED_TEST_RECIPIENTS
    if os.environ.get("OUTREACH_MAIL_PROVIDER", "outlook_desktop") == "outlook_desktop":
        raise RuntimeError("Authorized sender chen.yuxuan9@northeastern.edu uses Word Mail Merge for Mac and Outlook Desktop. This local connection cannot send unattended from Render. No cloud mail API is authorized; export the persisted Word/CSV batch or authorize an API connection separately.")
    from outreach_agent.gmail_integration import gmail_connection_status
    if os.environ.get("SEND_MODE", "dry_run") != "live":
        raise RuntimeError("SEND_MODE=live is required on the Outreach service; no message was sent")
    status = gmail_connection_status()
    if not status["authenticated"]:
        raise RuntimeError(status["prerequisite"])
    return GmailProviderAdapter(settings={}), PREFERRED_SENDER, DispatchConfig(allowed_recipients=frozenset(AUTHORIZED_TEST_RECIPIENTS))


def send_invitation_batch(batch_id: str, *, path=REGISTRY_DB, provider=None, sender=None, config=None):
    """The Send action is batch authorization; recipient restrictions still apply."""
    import json
    from dataclasses import asdict
    from outreach_agent.geo_engine_handoff import owner_fixture_matches
    init_registry(path)
    with _connect(path) as db:
        batch = db.execute("SELECT draft_ids_json FROM invitation_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("invitation batch not found")
        ids = json.loads(batch["draft_ids_json"])
        if not ids:
            raise ValueError("invitation batch contains no generated drafts")
        for draft_id in ids:
            draft = db.execute("SELECT * FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
            record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (draft["profile_id"],)).fetchone()
            if record["withdrawal_status"] == "WITHDRAWN" or record["consent_status"] == "DECLINED":
                raise PermissionError("Invitation blocked by withdrawal or declined participation")
            if not owner_fixture_matches(record, draft["recipient"]):
                raise PermissionError("Controlled staging dispatch is restricted to the two exact owner fixtures")
    if provider is None:
        try:
            provider, sender, config = configured_dispatch()
        except RuntimeError as exc:
            with _connect(path) as db:
                db.execute("UPDATE invitation_batches SET status='BLOCKED' WHERE batch_id=?", (batch_id,))
                for draft_id in ids:
                    key = f"blocked:{batch_id}:{draft_id}"
                    if not db.execute("SELECT 1 FROM email_events WHERE idempotency_key=?", (key,)).fetchone():
                        _event(db, draft_id, key, "DISPATCH_BLOCKED", "outlook_desktop", False, None, str(exc))
            raise
    outcomes = approve_and_dispatch_batch(ids, "EXPLICIT_DASHBOARD_SEND", provider=provider, sender=sender, path=path, config=config or DispatchConfig())
    with _connect(path) as db:
        db.execute("UPDATE invitation_batches SET status=? WHERE batch_id=?", ("SENT" if all(o.status in {"SENT", "DUPLICATE"} and o.message_id for o in outcomes) else "PARTIAL_OR_BLOCKED", batch_id))
    return {"batch_id": batch_id, "outcomes": [asdict(o) for o in outcomes], "delivery_confirmation": "NOT_CONFIRMED"}


def _utc_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


def _event(db, draft_id: str, key: str, event_type: str, provider: str, enabled: bool, message_id: str | None, error: str | None) -> str:
    event_id = "email-event-" + secrets.token_urlsafe(10)
    db.execute("INSERT INTO email_events(event_id,draft_id,event_type,provider,provider_enabled,message_id,error,created_at,idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)", (event_id, draft_id, event_type, provider, int(enabled), message_id, error, now(), key))
    return event_id


def dispatch_approved_drafts(draft_ids: list[str], *, provider: EmailProvider, sender: str,
                             path=REGISTRY_DB, config: DispatchConfig = DispatchConfig()) -> list[DispatchOutcome]:
    """Dispatch one or many approved drafts through exactly the same path.

    A provider result means accepted by the provider, not delivered to the
    recipient. The function never bypasses approval, suppression, allowlists,
    or the existing invitation lifecycle.
    """
    if len(draft_ids) > config.max_batch_size:
        raise ValueError("batch exceeds configured max_batch_size")
    if config.max_attempts < 1 or config.max_per_minute < 1:
        raise ValueError("dispatch limits must be positive")
    init_registry(path)
    outcomes: list[DispatchOutcome] = []
    with _connect(path) as db:
        for draft_id in draft_ids:
            draft = db.execute("SELECT * FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
            if not draft:
                outcomes.append(DispatchOutcome(draft_id, "NOT_FOUND", error="draft not found")); continue
            key = f"dispatch:{draft_id}"
            prior = db.execute("SELECT event_id,event_type,message_id,error FROM email_events WHERE draft_id=? AND event_type IN ('DISPATCH_ACCEPTED','MANUAL_SEND_RECONCILED','MANUAL_PROVIDER_ACCEPTED')", (draft_id,)).fetchone()
            if not prior:
                prior = db.execute("SELECT event_id,event_type,message_id,error FROM email_events WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                outcomes.append(DispatchOutcome(draft_id, "DUPLICATE", prior["event_id"], prior["message_id"], prior["error"])); continue
            if draft["approval_status"] != "APPROVED":
                outcomes.append(DispatchOutcome(draft_id, "BLOCKED_APPROVAL", error="human approval is required")); continue
            approved_report_bytes=None
            if draft["attachment_report_id"]:
                report=db.execute("SELECT report_id,profile_id,source_profile_version,status,document_base64 FROM research_reports WHERE report_id=?",
                    (draft["attachment_report_id"],)).fetchone()
                if not report or report["status"]!='READY' or not report["document_base64"]:
                    outcomes.append(DispatchOutcome(draft_id,"BLOCKED_APPROVAL",error="approved DOCX artifact is unavailable or incomplete"));continue
                try:approved_report_bytes=base64.b64decode(report["document_base64"],validate=True)
                except Exception:
                    outcomes.append(DispatchOutcome(draft_id,"BLOCKED_APPROVAL",error="stored DOCX artifact is invalid"));continue
                report_hash=hashlib.sha256(approved_report_bytes).hexdigest()
                review=db.execute("SELECT decision FROM research_report_reviews WHERE report_id=? AND document_sha256=? ORDER BY reviewed_at DESC LIMIT 1",
                    (report["report_id"],report_hash)).fetchone()
                expected_content_key=_hash(report_hash+"\0"+str(draft["subject"] or "")+"\0"+str(draft["body"] or ""))
                original=db.execute("SELECT s.invitation_recipient_email FROM geo_handoffs h JOIN questionnaire_submissions s ON s.submission_id=h.consent_submission_id WHERE h.profile_id=? AND h.profile_version=?",
                    (report["profile_id"],report["source_profile_version"])).fetchone()
                if (not review or review["decision"]!="APPROVED" or draft["preview_hash"]!=expected_content_key
                    or not original or str(original["invitation_recipient_email"] or "").casefold()!=str(draft["recipient"] or "").casefold()):
                    outcomes.append(DispatchOutcome(draft_id,"BLOCKED_APPROVAL",error="exact DOCX approval, content binding, or original recipient check failed"));continue
            if not provider.enabled:
                outcomes.append(DispatchOutcome(draft_id, "BLOCKED_PROVIDER", error="provider is disabled")); continue
            recipient = str(draft["recipient"]).casefold()
            if config.allowed_recipients is not None and recipient not in {item.casefold() for item in config.allowed_recipients}:
                outcomes.append(DispatchOutcome(draft_id, "BLOCKED_ALLOWLIST", error="recipient is not allowlisted")); continue
            if db.execute("SELECT 1 FROM suppressed_recipients WHERE recipient=?", (recipient,)).fetchone():
                outcomes.append(DispatchOutcome(draft_id, "BLOCKED_SUPPRESSED", error="recipient is suppressed")); continue
            recent = db.execute("SELECT COUNT(*) AS n FROM dispatch_attempts WHERE created_at>=? AND status IN ('PENDING','ACCEPTED','FAILED_RETRYABLE')", (_utc_cutoff(),)).fetchone()["n"]
            if recent >= config.max_per_minute:
                outcomes.append(DispatchOutcome(draft_id, "RATE_LIMITED", error="per-minute dispatch limit reached")); continue
            last_error = None
            for attempt in range(1, config.max_attempts + 1):
                attempt_id = "attempt-" + secrets.token_urlsafe(10)
                created = now()
                claimed = db.execute("INSERT INTO dispatch_attempts(attempt_id,draft_id,idempotency_key,provider,sender,recipient,attempt_number,status,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(draft_id,idempotency_key,attempt_number) DO NOTHING", (attempt_id, draft_id, key, provider.name, sender, draft["recipient"], attempt, "PENDING", None, created, created))
                db.commit()  # Durable claim precedes the irreversible provider call.
                if claimed.rowcount != 1:
                    outcomes.append(DispatchOutcome(draft_id, "IN_PROGRESS_OR_UNKNOWN", error="An existing attempt requires provider reconciliation; no resend attempted"))
                    break
                try:
                    if draft["attachment_report_id"]:
                        result = provider.send_document(sender=sender, recipient=draft["recipient"], subject=draft["subject"], body=draft["body"], filename=draft["attachment_report_id"] + ".docx", content=approved_report_bytes)
                    else:
                        result = provider.send(sender=sender, recipient=draft["recipient"], subject=draft["subject"], body=draft["body"])
                except EmailProviderError as exc:
                    last_error = str(exc)
                    status = "FAILED_RETRYABLE" if exc.retryable and attempt < config.max_attempts else "FAILED"
                    db.execute("UPDATE dispatch_attempts SET status=?,error=?,updated_at=? WHERE attempt_id=?", (status, last_error, now(), attempt_id))
                    if status == "FAILED_RETRYABLE":
                        if config.retry_backoff: config.retry_backoff(attempt)
                        continue
                    event_id = _event(db, draft_id, key, "DISPATCH_FAILED", provider.name, provider.enabled, None, last_error)
                    outcomes.append(DispatchOutcome(draft_id, "FAILED", event_id, error=last_error, attempts=attempt)); break
                except Exception as exc:  # provider boundary: persist unexpected failures too
                    last_error = str(exc)
                    db.execute("UPDATE dispatch_attempts SET status='FAILED',error=?,updated_at=? WHERE attempt_id=?", (last_error, now(), attempt_id))
                    event_id = _event(db, draft_id, key, "DISPATCH_FAILED", provider.name, provider.enabled, None, last_error)
                    outcomes.append(DispatchOutcome(draft_id, "FAILED", event_id, error=last_error, attempts=attempt)); break
                else:
                    db.execute("UPDATE dispatch_attempts SET status='ACCEPTED',provider_message_id=?,updated_at=? WHERE attempt_id=?", (result.message_id, now(), attempt_id))
                    event_id = _event(db, draft_id, key, "DISPATCH_ACCEPTED", provider.name, provider.enabled, result.message_id, None)
                    invitation_id = draft["invitation_id"]
                    if invitation_id:
                        db.execute("UPDATE questionnaire_invitations SET status='SENT' WHERE invitation_id=? AND status IN ('DRAFTED','INVITED')", (invitation_id,))
                    if not draft["attachment_report_id"]:
                        db.execute("UPDATE research_records SET recruitment_status='INVITED',promotion_state='INVITED',updated_at=? WHERE profile_id=?", (now(), draft["profile_id"]))
                    db.commit()
                    outcomes.append(DispatchOutcome(draft_id, "SENT", event_id, result.message_id, attempts=attempt)); break
            else:
                event_id = _event(db, draft_id, key, "DISPATCH_FAILED", provider.name, provider.enabled, None, last_error or "retry limit exhausted")
                outcomes.append(DispatchOutcome(draft_id, "RETRY_EXHAUSTED", event_id, error=last_error, attempts=config.max_attempts))
    return outcomes


def approve_and_dispatch_batch(draft_ids: list[str], reviewer: str, *, provider: EmailProvider, sender: str,
                               path=REGISTRY_DB, config: DispatchConfig = DispatchConfig()) -> list[DispatchOutcome]:
    """The production batch path: human approval followed by dispatch."""
    with _connect(path) as db:
        pending = [row["draft_id"] for draft_id in draft_ids for row in db.execute("SELECT draft_id FROM email_drafts WHERE draft_id=? AND approval_status='PENDING'", (draft_id,))]
    approve_email_batch(pending, reviewer, path=path)
    return dispatch_approved_drafts(draft_ids, provider=provider, sender=sender, path=path, config=config)
