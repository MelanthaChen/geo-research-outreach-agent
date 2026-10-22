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

from outreach_agent.research_lifecycle import REGISTRY_DB, _connect, init_registry, now, approve_email_batch


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
        return ProviderResult(message_id=str(result.get("message_id") or result.get("id") or "gmail-accepted"))


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
            key = f"dispatch:{draft_id}:{draft['preview_hash']}:{provider.name}"
            prior = db.execute("SELECT event_id,event_type,message_id,error FROM email_events WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                outcomes.append(DispatchOutcome(draft_id, "DUPLICATE", prior["event_id"], prior["message_id"], prior["error"])); continue
            if draft["approval_status"] != "APPROVED":
                outcomes.append(DispatchOutcome(draft_id, "BLOCKED_APPROVAL", error="human approval is required")); continue
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
                db.execute("INSERT INTO dispatch_attempts(attempt_id,draft_id,idempotency_key,provider,sender,recipient,attempt_number,status,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (attempt_id, draft_id, key, provider.name, sender, draft["recipient"], attempt, "PENDING", None, created, created))
                try:
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
                    db.execute("UPDATE research_records SET recruitment_status='INVITED',promotion_state='INVITED',updated_at=? WHERE profile_id=?", (now(), draft["profile_id"]))
                    outcomes.append(DispatchOutcome(draft_id, "SENT", event_id, result.message_id, attempts=attempt)); break
            else:
                event_id = _event(db, draft_id, key, "DISPATCH_FAILED", provider.name, provider.enabled, None, last_error or "retry limit exhausted")
                outcomes.append(DispatchOutcome(draft_id, "RETRY_EXHAUSTED", event_id, error=last_error, attempts=config.max_attempts))
    return outcomes


def approve_and_dispatch_batch(draft_ids: list[str], reviewer: str, *, provider: EmailProvider, sender: str,
                               path=REGISTRY_DB, config: DispatchConfig = DispatchConfig()) -> list[DispatchOutcome]:
    """The production batch path: human approval followed by dispatch."""
    approve_email_batch(draft_ids, reviewer, path=path)
    return dispatch_approved_drafts(draft_ids, provider=provider, sender=sender, path=path, config=config)
