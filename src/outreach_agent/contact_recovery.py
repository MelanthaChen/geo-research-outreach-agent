"""Bounded recovery of already-personalized records from stored first-party evidence."""

from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
from typing import Any

from outreach_agent.contact_eligibility import (
    contact_readiness_counts,
    evidence_fingerprint,
    first_party_business_email_evidence,
    eligible_business_email_channels,
)
from outreach_agent.research_lifecycle import REGISTRY_DB, _connect, init_registry, now


def _ensure_schema(path: Any) -> None:
    init_registry(path)
    with _connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS contact_recovery_runs (
          run_id TEXT PRIMARY KEY, max_candidates INTEGER NOT NULL,
          selected INTEGER NOT NULL DEFAULT 0, recovered INTEGER NOT NULL DEFAULT 0,
          pending INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contact_recovery_audit (
          recovery_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, profile_id TEXT NOT NULL,
          email TEXT, source_url TEXT, evidence_hash TEXT, method TEXT NOT NULL,
          outcome TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
          UNIQUE(run_id, profile_id, email, evidence_hash)
        );
        CREATE INDEX IF NOT EXISTS contact_recovery_run_idx ON contact_recovery_audit(run_id, outcome);
        """)


def recover_personalized_contacts(
    path: Any = REGISTRY_DB, *, limit: int = 25, profile_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Promote only exact, stored first-party email evidence; never fetch or call AI.

    A company is recovered only when it already has verified identity and
    eligibility, is not suppressed, and a saved first-party source excerpt
    contains the exact appropriate business email. Previously rejected
    channels are never revived. Completed personalization is left untouched.
    """
    if not 1 <= int(limit) <= 100:
        raise ValueError("contact recovery limit must be between 1 and 100")
    ids = list(dict.fromkeys(str(value).strip() for value in (profile_ids or []) if str(value).strip()))
    if len(ids) > limit:
        raise ValueError("requested profile_ids exceed the bounded recovery limit")
    _ensure_schema(path)
    run_id = "contact-recovery-" + secrets.token_hex(10)
    stamp = now()
    recovered = pending = 0
    with _connect(path) as db:
        db.execute("INSERT INTO contact_recovery_runs(run_id,max_candidates,created_at) VALUES (?,?,?)", (run_id, limit, stamp))
        where = [
            "profile_type<>'owner_controlled_test'", "personalization_status='COMPLETE'",
            "identity_verification_status='VERIFIED'", "qualification_status IN ('ELIGIBLE','QUALIFIED')",
            "suppression_status<>'SUPPRESSED'", "withdrawal_status<>'WITHDRAWN'",
            "consent_status NOT IN ('DECLINED','WITHDRAWN')", "recruitment_status NOT IN ('EXCLUDED','UNSUBSCRIBED')",
            "contact_verification_status<>'CONTACT_VERIFIED'",
        ]
        params: list[Any] = []
        if ids:
            marks = ",".join("?" for _ in ids)
            where.append(f"profile_id IN ({marks})")
            params.extend(ids)
        params.append(limit)
        rows = db.execute("SELECT * FROM research_records WHERE " + " AND ".join(where) + " ORDER BY updated_at,profile_id LIMIT ?", params).fetchall()
        db.execute("UPDATE contact_recovery_runs SET selected=? WHERE run_id=?", (len(rows), run_id))
        for row in rows:
            record = dict(row)
            channels = json.loads(record.get("contact_channels_json") or "[]")
            if not isinstance(channels, list):
                channels = []
            evidence_items = first_party_business_email_evidence(record)
            eligible = []
            for item in evidence_items:
                status = str(item.get("review_status") or "PENDING").upper()
                if status in {"REJECTED", "MAYBE", "DO_NOT_USE", "BLOCKED"}:
                    continue
                # The exact email must be visible in saved first-party evidence.
                # This is the recovery verification rule; there is no DNS/mailbox
                # probe, outbound email, or inferred address generation.
                fingerprint = evidence_fingerprint(str(record["profile_id"]), item)
                eligible.append(item)
            if eligible:
                promote = {evidence_fingerprint(str(record["profile_id"]), item) for item in eligible}
                for channel in channels:
                    if not isinstance(channel, dict):
                        continue
                    item = next((candidate for candidate in eligible
                                 if str(channel.get("email") or "").strip().casefold() == candidate["email"]
                                 and str(channel.get("source_url") or channel.get("channel_url") or "").strip() == candidate["source_url"]
                                 and str(channel.get("evidence") or channel.get("evidence_text") or channel.get("excerpt") or "") == candidate["evidence"]), None)
                    if not item:
                        continue
                    fingerprint = evidence_fingerprint(str(record["profile_id"]), item)
                    if fingerprint not in promote:
                        continue
                    channel.update({
                        "review_status": "VERIFIED",
                        "verification_status": "VERIFIED",
                        "verification_method": "STORED_FIRST_PARTY_EXACT_EMAIL_EVIDENCE",
                        "verified_at": stamp,
                        "verification_evidence_hash": fingerprint,
                    })
                    db.execute("INSERT INTO contact_recovery_audit(recovery_id,run_id,profile_id,email,source_url,evidence_hash,method,outcome,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,profile_id,email,evidence_hash) DO NOTHING", (
                        "contact-audit-" + hashlib.sha256((run_id + fingerprint).encode()).hexdigest()[:24], run_id, record["profile_id"], item["email"], item["source_url"], fingerprint,
                        "STORED_FIRST_PARTY_EXACT_EMAIL_EVIDENCE", "RECOVERED", "Exact business email appears in first-party evidence for the verified official website", stamp,
                    ))
                db.execute("UPDATE research_records SET contact_channels_json=?,contact_verification_status='CONTACT_VERIFIED',outreach_ready_status=CASE WHEN outreach_ready_status='NOT_READY' THEN 'READY_FOR_REVIEW' ELSE outreach_ready_status END,updated_at=? WHERE profile_id=?", (json.dumps(channels, ensure_ascii=False, sort_keys=True), stamp, record["profile_id"]))
                recovered += 1
            else:
                db.execute("INSERT INTO contact_recovery_audit(recovery_id,run_id,profile_id,method,outcome,reason,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(run_id,profile_id,email,evidence_hash) DO NOTHING", (
                    "contact-audit-" + secrets.token_hex(12), run_id, record["profile_id"], "STORED_FIRST_PARTY_EVIDENCE_REVIEW", "PENDING",
                    "No approved-purpose exact business email could be verified from stored first-party evidence", stamp,
                ))
                pending += 1
        db.execute("UPDATE contact_recovery_runs SET recovered=?,pending=? WHERE run_id=?", (recovered, pending, run_id))
    return {"run_id": run_id, "selected": len(rows), "recovered": recovered, "pending": pending, "paid_calls": 0, "network_requests": 0}


def contact_readiness_summary(path: Any = REGISTRY_DB) -> dict[str, int]:
    _ensure_schema(path)
    with _connect(path) as db:
        rows = db.execute("SELECT * FROM research_records WHERE profile_type<>'owner_controlled_test'").fetchall()
    return contact_readiness_counts([dict(row) for row in rows])
