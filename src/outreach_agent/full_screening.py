"""Durable, no-paid-provider full-pool screening orchestration.

This is deliberately a coordinator around the existing research lifecycle.  It
does not run GEO Engine audits or experiments, and it never treats a mock
response as an AI screening result.  The first pass records deterministic
eligibility for every normal registry record and pauses at the AI boundary
when no approved local/free provider is configured.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import secrets
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.research_lifecycle import REGISTRY_DB, init_registry, now
from outreach_agent.screening_provider import PROMPT_VERSION, call as call_ai, configured_provider, estimate_cost

RUN_VERSION = "full-us-outreach-screening/v2-soft-size"
EMAIL_TYPES = {"GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "NAMED_PERSON_EMAIL"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "data" / "full_screening"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _channels(row: sqlite3.Row) -> list[dict[str, Any]]:
    values = _json(row["contact_channels_json"], [])
    return values if isinstance(values, list) else []


def _email_channels(row: sqlite3.Row) -> list[dict[str, Any]]:
    result = []
    for item in _channels(row):
        if str(item.get("review_status", "PENDING")).upper() == "REJECTED":
            continue
        if str(item.get("channel_type", "")).upper() in EMAIL_TYPES and EMAIL_RE.match(str(item.get("email") or "")):
            result.append(item)
    return result


def deterministic_gate(row: sqlite3.Row) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Return eligibility, explicit blockers, and preserved email evidence."""
    if row["profile_type"] == "owner_controlled_test":
        return "EXCLUDED_OWNER_CONTROLLED_TEST", ["OWNER_CONTROLLED_TEST"], []
    reasons: list[str] = []
    if row["geographic_classification"] != "VERIFIED_US":
        reasons.append(f"GEOGRAPHY_{row['geographic_classification']}")
    if row["identity_verification_status"] != "VERIFIED":
        reasons.append(f"IDENTITY_{row['identity_verification_status']}")
    if row["qualification_status"] != "ELIGIBLE":
        reasons.append("QUALIFICATION_NOT_ELIGIBLE")
    if not str(row["official_website"] or "").strip():
        reasons.append("MISSING_OFFICIAL_WEBSITE")
    # Size is retained as a preference signal only. Missing employee/revenue
    # evidence must not block a verified US business from screening.
    emails = _email_channels(row)
    if not emails:
        reasons.append("NO_VALID_PUBLIC_BUSINESS_EMAIL")
    if row["consent_status"] in {"DECLINED", "WITHDRAWN"}:
        reasons.append("CONSENT_SUPPRESSED")
    if row["recruitment_status"] in {"EXCLUDED", "UNSUBSCRIBED"}:
        reasons.append("RECRUITMENT_SUPPRESSED")
    return ("ELIGIBLE_FOR_AI_SCREENING" if not reasons else "BLOCKED_DETERMINISTIC"), reasons, emails


def _schema(path: Path) -> None:
    init_registry(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
        CREATE TABLE IF NOT EXISTS full_screening_jobs (
          job_id TEXT PRIMARY KEY, run_version TEXT NOT NULL, status TEXT NOT NULL,
          provider TEXT NOT NULL, provider_mode TEXT NOT NULL, created_at TEXT NOT NULL,
          started_at TEXT, completed_at TEXT, processed INTEGER NOT NULL DEFAULT 0,
          deterministic_eligible INTEGER NOT NULL DEFAULT 0, ai_screened INTEGER NOT NULL DEFAULT 0,
          ai_blocked INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
          token_input INTEGER NOT NULL DEFAULT 0, token_output INTEGER NOT NULL DEFAULT 0,
          estimated_cost_usd REAL NOT NULL DEFAULT 0, error_summary_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS full_screening_queue (
          job_id TEXT NOT NULL, profile_id TEXT NOT NULL, cohort TEXT NOT NULL,
          eligibility_status TEXT NOT NULL, blocking_reasons_json TEXT NOT NULL DEFAULT '[]',
          email_evidence_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, completed_at TEXT,
          PRIMARY KEY(job_id, profile_id)
        );
        CREATE TABLE IF NOT EXISTS full_screening_content_cache (
          content_hash TEXT PRIMARY KEY, profile_id TEXT NOT NULL, official_website TEXT NOT NULL,
          observed_at TEXT NOT NULL, evidence_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS full_screening_queue_status_idx ON full_screening_queue(job_id,status);
        """)


def plan_full_run(path: Path = REGISTRY_DB, *, run_key: str = RUN_VERSION) -> str:
    """Create or resume the single all-record job without touching source DBs."""
    _schema(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        prior = db.execute("SELECT job_id FROM full_screening_jobs WHERE run_version=? ORDER BY created_at DESC LIMIT 1", (run_key,)).fetchone()
        if prior:
            return str(prior[0])
        job_id = "full-screening-" + secrets.token_urlsafe(10)
        db.execute("INSERT INTO full_screening_jobs(job_id,run_version,status,provider,provider_mode,created_at) VALUES (?,?,?,?,?,?)", (job_id, run_key, "PLANNED", "UNCONFIGURED", "NO_PAID_CALLS", now()))
        rows = db.execute("SELECT * FROM research_records ORDER BY profile_id").fetchall()
        eligible = 0
        for row in rows:
            status, blockers, emails = deterministic_gate(row)
            if status == "ELIGIBLE_FOR_AI_SCREENING": eligible += 1
            db.execute("INSERT OR IGNORE INTO full_screening_queue(job_id,profile_id,cohort,eligibility_status,blocking_reasons_json,email_evidence_json,status) VALUES (?,?,?,?,?,?,?)", (job_id, row["profile_id"], row["profile_type"], status, json.dumps(blockers, sort_keys=True), json.dumps(emails, sort_keys=True), "PENDING_AI" if status == "ELIGIBLE_FOR_AI_SCREENING" else "BLOCKED_DETERMINISTIC"))
            evidence = _json(row["verification_evidence_json"], [])
            source = _json(row["provenance_json"], [])
            # Existing observed hashes are reusable cache inputs, not new claims.
            for item in evidence if isinstance(evidence, list) else []:
                if isinstance(item, dict) and item.get("content_hash"):
                    db.execute("INSERT OR IGNORE INTO full_screening_content_cache VALUES (?,?,?,?,?)", (item["content_hash"], row["profile_id"], row["official_website"], now(), json.dumps(item, sort_keys=True)))
            if isinstance(source, list):
                for item in source:
                    if isinstance(item, dict) and item.get("content_hash"):
                        db.execute("INSERT OR IGNORE INTO full_screening_content_cache VALUES (?,?,?,?,?)", (item["content_hash"], row["profile_id"], row["official_website"], now(), json.dumps(item, sort_keys=True)))
        db.execute("UPDATE full_screening_jobs SET deterministic_eligible=? WHERE job_id=?", (eligible, job_id))
    return job_id


def detect_provider() -> str:
    """Only an explicitly configured local/free provider may cross the boundary."""
    if configured_provider() is not None:
        return "OPENAI_COMPATIBLE"
    if os.getenv("GEO_SCREENING_PROVIDER", "").strip().lower() in {"local", "ollama", "vllm"}:
        return os.environ["GEO_SCREENING_PROVIDER"].upper()
    return "UNCONFIGURED"


def run_full_job(job_id: str, path: Path = REGISTRY_DB) -> dict[str, Any]:
    _schema(path)
    provider = detect_provider()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        job = db.execute("SELECT * FROM full_screening_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            raise KeyError(job_id)
        pending = db.execute("SELECT COUNT(*) FROM full_screening_queue WHERE job_id=? AND status='PENDING_AI'", (job_id,)).fetchone()[0]
        db.execute("UPDATE full_screening_jobs SET status=?,provider=?,started_at=?,ai_blocked=? WHERE job_id=?", ("PAUSED_AT_AI_BOUNDARY" if provider == "UNCONFIGURED" and pending else "COMPLETE", provider, now(), pending if provider == "UNCONFIGURED" else 0, job_id))
        if provider == "UNCONFIGURED":
            db.execute("UPDATE full_screening_queue SET status='BLOCKED_AI_PROVIDER',last_error=? WHERE job_id=? AND status='PENDING_AI'", ("No approved local/free AI provider configured; paid providers intentionally not invoked", job_id))
            db.execute("UPDATE full_screening_jobs SET completed_at=?,processed=(SELECT COUNT(*) FROM full_screening_queue WHERE job_id=?),error_summary_json=? WHERE job_id=?", (now(), job_id, json.dumps({"AI_PROVIDER_UNCONFIGURED": pending}), job_id))
            return {"job_id": job_id, "status": "PAUSED_AT_AI_BOUNDARY", "deterministic_eligible": int(job["deterministic_eligible"]), "ai_blocked": pending, "provider": provider, "paid_calls": 0}
        if provider == "OPENAI_COMPATIBLE":
            config = configured_provider()
            if config is None:
                raise RuntimeError("AI_PROVIDER_NOT_CONFIGURED")
            from outreach_agent.preliminary_screening import ScreeningConfig, _fetch
            queue = db.execute("SELECT q.profile_id,r.profile_version,r.name,r.industry,r.location,r.official_website FROM full_screening_queue q JOIN research_records r USING(profile_id) WHERE q.job_id=? AND q.status='PENDING_AI' ORDER BY q.profile_id", (job_id,)).fetchall()
            processed = success = failed = input_tokens = output_tokens = 0
            for item in queue:
                try:
                    pages, meta = _fetch(item["official_website"], ScreeningConfig(), mock=False, name=item["name"], industry=item["industry"])
                    result, usage = call_ai(config, name=item["name"], industry=item["industry"], location=item["location"], pages=pages)
                    rating = str(result.get("rating", "INSUFFICIENT_EVIDENCE")).upper()
                    if rating not in {"HIGH", "MEDIUM", "LOW", "INSUFFICIENT_EVIDENCE"}:
                        raise ValueError("AI_RESULT_INVALID_RATING")
                    evidence = [{"url": page.get("url", ""), "excerpt": page.get("text", "")[:800], "captured_at": now(), "content_hash": _hash(page.get("text", "")), "deterministic_checks": meta} for page in pages]
                    screening_id = "screening-" + _hash(item["profile_id"] + json.dumps(result, sort_keys=True))[:20]
                    db.execute("INSERT OR IGNORE INTO screenings VALUES (?,?,?,?,?,?,?,?,?,?,?)", (screening_id, item["profile_id"], item["profile_version"], "PRELIMINARY_UNVALIDATED", json.dumps({"rating": rating, **result}, sort_keys=True), json.dumps(evidence, sort_keys=True), "OPENAI_COMPATIBLE", config.model, PROMPT_VERSION, RUN_VERSION, now()))
                    ready = "READY_FOR_EMAIL" if rating in {"HIGH", "MEDIUM"} else "NOT_READY"
                    reasons = [] if ready == "READY_FOR_EMAIL" else [f"GEO_POTENTIAL_{rating}"]
                    db.execute("UPDATE research_records SET screening_status='SCREENING_COMPLETE',screening_provider=?,screening_model=?,screening_prompt_version=?,screening_completed_at=?,outreach_ready_status=?,readiness_reason_json=?,updated_at=? WHERE profile_id=?", ("OPENAI_COMPATIBLE", config.model, PROMPT_VERSION, now(), ready, json.dumps(reasons), now(), item["profile_id"]))
                    db.execute("UPDATE full_screening_queue SET status='COMPLETE',attempts=attempts+1,completed_at=? WHERE job_id=? AND profile_id=?", (now(), job_id, item["profile_id"]))
                    input_tokens += usage["input_tokens"]; output_tokens += usage["output_tokens"]; success += 1
                except Exception as exc:
                    db.execute("UPDATE full_screening_queue SET status='FAILED',attempts=attempts+1,last_error=? WHERE job_id=? AND profile_id=?", (f"{type(exc).__name__}:{exc}", job_id, item["profile_id"]))
                    failed += 1
                processed += 1
            cost = input_tokens / 1_000_000 * config.input_cost_per_million + output_tokens / 1_000_000 * config.output_cost_per_million
            status = "COMPLETE" if failed == 0 else "COMPLETE_WITH_FAILURES"
            db.execute("UPDATE full_screening_jobs SET status=?,completed_at=?,processed=?,ai_screened=?,failed=?,token_input=?,token_output=?,estimated_cost_usd=?,error_summary_json=? WHERE job_id=?", (status, now(), processed, success, failed, input_tokens, output_tokens, cost, json.dumps({"failed": failed}), job_id))
            return {"job_id": job_id, "status": status, "processed": processed, "ai_screened": success, "failed": failed, "token_input": input_tokens, "token_output": output_tokens, "estimated_cost_usd": cost, "provider": provider, "paid_calls": processed}
        raise RuntimeError(f"Provider {provider} is declared but no safe adapter is installed")


def export_full_run(job_id: str, path: Path = REGISTRY_DB, output_dir: Path = OUTPUT_DIR) -> dict[str, str]:
    _schema(path); output_dir.mkdir(parents=True, exist_ok=True)
    columns = ["profile_id", "cohort", "name", "industry", "location", "official_website", "geographic_classification", "identity_verification_status", "qualification_status", "contact_status", "email", "email_channel_type", "email_source_url", "email_evidence", "size_status", "size_preference", "eligibility_status", "blocking_reasons", "screening_status", "opportunity_level", "ai_provider", "ai_model", "token_input", "token_output", "estimated_cost_usd"]
    csv_path = output_dir / "full_us_outreach_screening.csv"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT q.*,r.name,r.industry,r.location,r.official_website,r.geographic_classification,r.identity_verification_status,r.qualification_status,r.contact_verification_status,r.size_status FROM full_screening_queue q JOIN research_records r USING(profile_id) WHERE q.job_id=? ORDER BY q.cohort,q.profile_id", (job_id,)).fetchall()
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n"); writer.writeheader()
        for row in rows:
            emails = _json(row["email_evidence_json"], []); email = emails[0] if emails else {}
            size = str(row["size_status"] or "SIZE_UNKNOWN"); size_pref = "PREFERRED" if size.upper() not in {"", "UNKNOWN", "SIZE_UNKNOWN"} else "NO_SIZE_PREFERENCE_EVIDENCE"
            writer.writerow({"profile_id":row["profile_id"],"cohort":row["cohort"],"name":row["name"],"industry":row["industry"] or "","location":row["location"] or "","official_website":row["official_website"],"geographic_classification":row["geographic_classification"],"identity_verification_status":row["identity_verification_status"],"qualification_status":row["qualification_status"],"contact_status":row["contact_verification_status"],"email":email.get("email", ""),"email_channel_type":email.get("channel_type", ""),"email_source_url":email.get("source_url", ""),"email_evidence":email.get("evidence", ""),"size_status":size,"size_preference":size_pref,"eligibility_status":row["eligibility_status"],"blocking_reasons":";".join(_json(row["blocking_reasons_json"], [])),"screening_status":row["status"],"opportunity_level":"UNAVAILABLE","ai_provider":"UNCONFIGURED","ai_model":"","token_input":0,"token_output":0,"estimated_cost_usd":0})
    return {"csv": str(csv_path), "rows": str(len(rows))}


def summarize(job_id: str, path: Path = REGISTRY_DB) -> dict[str, Any]:
    _schema(path)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        job = dict(db.execute("SELECT * FROM full_screening_jobs WHERE job_id=?", (job_id,)).fetchone())
        counts = {f"{r[0]}:{r[1]}": r[2] for r in db.execute("SELECT cohort,status,COUNT(*) FROM full_screening_queue WHERE job_id=? GROUP BY cohort,status", (job_id,))}
        blockers = dict(db.execute("SELECT blocking_reasons_json,COUNT(*) FROM full_screening_queue WHERE job_id=? GROUP BY blocking_reasons_json ORDER BY COUNT(*) DESC LIMIT 25", (job_id,)))
        pending = int(db.execute("SELECT COUNT(*) FROM full_screening_queue WHERE job_id=? AND eligibility_status='ELIGIBLE_FOR_AI_SCREENING'", (job_id,)).fetchone()[0])
    job["counts"] = counts; job["top_blocker_sets"] = blockers
    job["pending_workload_estimate"] = estimate_cost(pending, config=configured_provider())
    return job

