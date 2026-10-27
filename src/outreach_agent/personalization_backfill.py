"""Resumable, rule-based personalization backfill for the Outreach registry.

This is deliberately a pre-consent extension of the existing lifecycle store.
It reuses the bounded preliminary-screening fetcher and never creates an
invitation, changes contact verification, calls a paid provider, or starts a
GEO Engine job.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from outreach_agent.combined_screening import _rule_based
from outreach_agent.preliminary_screening import ScreeningConfig, _fetch
from outreach_agent.research_lifecycle import REGISTRY_DB, _connect, init_registry, now


JOB_VERSION = "personalization-backfill/v1"


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _ensure_schema(path: Path | str) -> None:
    init_registry(path)
    with _connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(research_records)")}
        for name, definition in {
            "personalization_status": "TEXT NOT NULL DEFAULT 'NOT_STARTED'",
            "personalization_json": "TEXT NOT NULL DEFAULT '{}'",
            "personalization_completed_at": "TEXT",
            "personalization_error": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE research_records ADD COLUMN {name} {definition}")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS personalization_backfill_jobs (
          job_id TEXT PRIMARY KEY, version TEXT NOT NULL, status TEXT NOT NULL,
          target_db TEXT NOT NULL, mode TEXT NOT NULL, candidate_limit INTEGER NOT NULL,
          request_budget INTEGER NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
          completed_at TEXT, processed INTEGER NOT NULL DEFAULT 0,
          succeeded INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
          last_profile_id TEXT, error_summary_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS personalization_backfill_queue (
          job_id TEXT NOT NULL, profile_id TEXT NOT NULL, status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, started_at TEXT, completed_at TEXT,
          analysis_id TEXT, last_error TEXT,
          PRIMARY KEY(job_id, profile_id)
        );
        CREATE INDEX IF NOT EXISTS personalization_queue_status_idx
          ON personalization_backfill_queue(job_id, status);
        """)


def _json(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, ValueError):
        return default


def _actionable(record: Any) -> bool:
    """Require first-party, same-domain actionable evidence, not a raw email string."""
    website = str(record["official_website"] or "")
    host = (urlparse(website).hostname or "").casefold().removeprefix("www.")
    channels = _json(record["contact_channels_json"], [])
    if not isinstance(channels, list):
        return False
    unsuitable = {"privacy", "legal", "abuse", "careers", "jobs", "hr", "support"}
    for item in channels:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel_type") or "").upper()
        email = str(item.get("email") or "").strip().casefold()
        source = str(item.get("source_url") or "")
        source_host = (urlparse(source).hostname or "").casefold().removeprefix("www.")
        purpose = str(item.get("purpose") or "").casefold()
        if channel in {"FORM_ONLY", "EMAIL_AND_FORM", "CONTACT_FORM"} and source_host == host:
            return True
        if channel in {"EMAIL_ONLY", "EMAIL_AND_FORM", "GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "PARTNERSHIP_EMAIL", "NAMED_PERSON_EMAIL"} and "@" in email:
            local, domain = email.rsplit("@", 1)
            if domain == host and not any(token in local or token in purpose for token in unsuitable):
                if source_host == host:
                    return True
    return False


def eligible_profile_ids(path: Path | str, *, limit: int | None = None) -> list[str]:
    _ensure_schema(path)
    sql = """SELECT profile_id FROM research_records
      WHERE profile_type <> 'owner_controlled_test'
        AND geographic_classification='VERIFIED_US'
        AND identity_verification_status='VERIFIED'
        AND qualification_status IN ('ELIGIBLE','QUALIFIED')
        AND official_website <> ''
        AND suppression_status='NOT_SUPPRESSED'
        AND withdrawal_status='NOT_WITHDRAWN'
      ORDER BY profile_id"""
    if limit is not None:
        sql += " LIMIT ?"
    with _connect(path) as db:
        rows = db.execute(sql, (limit,) if limit is not None else ()).fetchall()
        result: list[str] = []
        for row in rows:
            record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (row[0],)).fetchone()
            if record is not None and _actionable(record):
                result.append(str(row[0]))
        return result


def create_job(path: Path | str, *, limit: int | None = None, request_budget: int = 100, job_id: str | None = None) -> str:
    _ensure_schema(path)
    ids = eligible_profile_ids(path, limit=limit)
    job_id = job_id or "personalization-" + uuid.uuid4().hex[:16]
    with _connect(path) as db:
        existing = db.execute("SELECT job_id FROM personalization_backfill_jobs WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            return job_id
        stamp = now()
        db.execute("""INSERT INTO personalization_backfill_jobs
          (job_id,version,status,target_db,mode,candidate_limit,request_budget,created_at)
          VALUES (?,?,?,?,?,?,?,?)""", (job_id, JOB_VERSION, "PLANNED", str(path), "RULE_BASED", len(ids), request_budget, stamp))
        db.executemany("INSERT INTO personalization_backfill_queue(job_id,profile_id,status) VALUES (?,?,?)", [(job_id, item, "PENDING") for item in ids])
    return job_id


def _profile_payload(record: Any, pages: list[dict[str, str]], fetched: dict[str, str], observed_at: str) -> dict[str, Any]:
    screening = _rule_based(name=record["name"], industry=record["industry"], location=record["location"], pages=pages)
    page_count = len(pages)
    text = " ".join(str(page.get("text") or "") for page in pages)
    description = " ".join(text.split())[:600] if text.strip() else "Not established from the bounded public-page sample."
    strength = "The bounded sample contains factual business and service information." if screening["signals"].get("factual_content") else "The bounded sample provides limited factual detail; strength is not established."
    findings = []
    for key, label in (("question_answerability", "pages with question-answerable information"), ("structured_information", "pages with factual contact/location information"), ("service_discoverability", "pages with explicit service or product language")):
        numerator = sum(bool(repr_page.get("text") and _rule_based(name=record["name"], industry=record["industry"], location=record["location"], pages=[repr_page])["signals"].get(key)) for repr_page in pages)
        if page_count:
            findings.append({"observation": label, "numerator": numerator, "denominator": page_count, "percentage": round(100 * numerator / page_count, 1), "calculation": f"100 * {numerator} / {page_count}", "source_urls": [str(page.get("url") or "") for page in pages], "observed_at": observed_at, "data_classification": "OBSERVED"})
    opportunities = []
    for item in screening["opportunities"][:2]:
        opportunities.append({**item, "source_urls": [str(page.get("url") or "") for page in pages], "observed_at": observed_at, "data_classification": "OBSERVED"})
    return {"version": "research-personalization/v1", "data_classification": "OBSERVED", "business_description": description, "company_strength": strength, "findings": findings, "recommendations": opportunities, "rating": screening["rating"], "signals": screening["signals"], "source_urls": [str(page.get("url") or "") for page in pages], "retrieved_at": observed_at, "fetch": fetched, "limitations": ["Bounded deterministic website sample; no LLM visibility test or GEO Engine experiment was run."]}


def run_job(job_id: str, path: Path | str, *, config: ScreeningConfig | None = None, fetcher: Callable[..., tuple[list[dict[str, str]], dict[str, str]]] | None = None, pause_after: int | None = None) -> dict[str, Any]:
    _ensure_schema(path)
    fetcher = fetcher or _fetch
    config = config or ScreeningConfig()
    with _connect(path) as db:
        job = db.execute("SELECT * FROM personalization_backfill_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job:
            raise KeyError(job_id)
        db.execute("UPDATE personalization_backfill_jobs SET status='RUNNING',started_at=COALESCE(started_at,?) WHERE job_id=? AND status IN ('PLANNED','PAUSED','RUNNING')", (now(), job_id))
        # Publish the running state before the first potentially slow network
        # request so an operator can observe and resume the job.
        db.commit()
        queue = db.execute("SELECT q.*,r.* FROM personalization_backfill_queue q JOIN research_records r USING(profile_id) WHERE q.job_id=? AND q.status IN ('PENDING','RETRY') ORDER BY q.profile_id", (job_id,)).fetchall()
        counts = Counter(); processed = 0
        for item in queue:
            if pause_after is not None and processed >= pause_after:
                break
            profile_id = str(item["profile_id"]); started = now()
            db.execute("UPDATE personalization_backfill_queue SET status='RUNNING',attempts=attempts+1,started_at=? WHERE job_id=? AND profile_id=?", (started, job_id, profile_id))
            try:
                pages, meta = fetcher(item["official_website"], config, mock=False, name=item["name"], industry=item["industry"])
                if not pages:
                    raise RuntimeError("NO_PUBLIC_PAGES")
                observed = now(); payload = _profile_payload(item, pages, meta, observed)
                if not payload["recommendations"]:
                    raise ValueError("NO_DEFENSIBLE_IMPROVEMENT_OBSERVATION")
                content_hash = _sha({"profile_id": profile_id, "pages": [(p.get("url"), p.get("text")) for p in pages]})
                analysis_id = "analysis-" + hashlib.sha256((profile_id + content_hash + "RULE_BASED").encode()).hexdigest()[:20]
                evidence = [{"url": p.get("url", ""), "excerpt": str(p.get("text") or "")[:800], "captured_at": observed, "content_hash": hashlib.sha256(str(p.get("text") or "").encode()).hexdigest(), "fetch": meta} for p in pages]
                screening_id = "screening-" + hashlib.sha256((profile_id + content_hash).encode()).hexdigest()[:20]
                db.execute("INSERT OR IGNORE INTO screening_analysis_results(analysis_id,profile_id,profile_version,mode,content_hash,result_json,input_tokens,output_tokens,estimated_cost_usd,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (analysis_id, profile_id, item["profile_version"], "RULE_BASED", content_hash, json.dumps({"pages": pages, "evidence": evidence, "profile": payload}, ensure_ascii=False, sort_keys=True), 0, 0, 0, observed))
                db.execute("INSERT OR IGNORE INTO screenings(screening_id,profile_id,profile_version,status,opportunities_json,evidence_json,provider,model,prompt_version,methodology,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (screening_id, profile_id, item["profile_version"], "PRELIMINARY_UNVALIDATED", json.dumps(payload["recommendations"], ensure_ascii=False, sort_keys=True), json.dumps(evidence, ensure_ascii=False, sort_keys=True), "RULE_BASED", "none", "rule-based-preliminary/v1", JOB_VERSION, observed))
                db.execute("UPDATE research_records SET screening_status='SCREENING_COMPLETE',screening_provider='RULE_BASED',screening_model='none',screening_prompt_version='rule-based-preliminary/v1',screening_completed_at=?,personalization_status='COMPLETE',personalization_json=?,personalization_completed_at=?,personalization_error=NULL,updated_at=? WHERE profile_id=?", (observed, json.dumps(payload, ensure_ascii=False, sort_keys=True), observed, observed, profile_id))
                db.execute("UPDATE personalization_backfill_queue SET status='COMPLETE',completed_at=?,analysis_id=?,last_error=NULL WHERE job_id=? AND profile_id=?", (observed, analysis_id, job_id, profile_id))
                counts["succeeded"] += 1
            except Exception as exc:
                db.execute("UPDATE personalization_backfill_queue SET status='FAILED',completed_at=?,last_error=? WHERE job_id=? AND profile_id=?", (now(), f"{type(exc).__name__}:{exc}", job_id, profile_id))
                db.execute("UPDATE research_records SET personalization_status='FAILED',personalization_error=?,updated_at=? WHERE profile_id=?", (f"{type(exc).__name__}:{exc}", now(), profile_id))
                counts["failed"] += 1
            # Each candidate is its own recovery boundary.  A timeout or
            # process restart must not hide all prior progress in one long
            # transaction.
            db.commit()
            processed += 1
        remaining = db.execute("SELECT COUNT(*) FROM personalization_backfill_queue WHERE job_id=? AND status IN ('PENDING','RETRY','RUNNING')", (job_id,)).fetchone()[0]
        status = "PAUSED" if pause_after is not None and remaining else ("COMPLETE" if not remaining else "RUNNING")
        db.execute("UPDATE personalization_backfill_jobs SET status=?,completed_at=?,processed=processed+?,succeeded=succeeded+?,failed=failed+?,last_profile_id=?,error_summary_json=? WHERE job_id=?", (status, now() if status == "COMPLETE" else None, processed, counts["succeeded"], counts["failed"], queue[min(processed, len(queue))-1]["profile_id"] if processed else None, json.dumps(dict(counts), sort_keys=True), job_id))
    return {"job_id": job_id, "status": status, "processed": processed, "remaining": remaining, "succeeded": counts["succeeded"], "failed": counts["failed"]}


def save_observed_personalization(profile_id: str, payload: dict, pages: list[dict], *, path=REGISTRY_DB) -> str:
    """Save a reviewed lightweight observation in the existing screening store.

    Used for bounded browser-rendered evidence when the existing HTTP fetcher
    cannot read a client-rendered site. No invitation or GEO job is created.
    """
    from outreach_agent.research_lifecycle import get_record, stored_invitation_personalization
    _ensure_schema(path)
    record = get_record(profile_id, path)
    stored_invitation_personalization({**record, "personalization_json": json.dumps(payload)})
    observed = payload['retrieved_at']
    content_hash = _sha({"pages": pages, "profile": payload})
    analysis_id = "analysis-" + _sha([profile_id, content_hash])[:20]
    evidence = [{"url": p['url'], "excerpt": p['text'], "captured_at": observed, "content_hash": _sha(p['text']), "method": "BOUNDED_BROWSER_OBSERVATION"} for p in pages]
    with _connect(path) as db:
        db.execute("INSERT INTO screening_analysis_results(analysis_id,profile_id,profile_version,mode,content_hash,result_json,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(profile_id,content_hash,mode) DO NOTHING", (analysis_id, profile_id, record['profile_version'], 'RULE_BASED', content_hash, json.dumps({"pages": pages, "evidence": evidence, "profile": payload}, sort_keys=True), observed))
        db.execute("INSERT INTO screenings(screening_id,profile_id,profile_version,status,opportunities_json,evidence_json,provider,model,prompt_version,methodology,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(screening_id) DO NOTHING", ('screening-'+content_hash[:20], profile_id, record['profile_version'], 'PRELIMINARY_OBSERVED', json.dumps(payload['recommendations']), json.dumps(evidence), 'BOUNDED_BROWSER_OBSERVATION', 'none', 'research-personalization/v1', 'Read-only rendered page sample; no GEO analysis or AI visibility experiment', observed))
        db.execute("UPDATE research_records SET personalization_status='COMPLETE',personalization_json=?,personalization_completed_at=?,personalization_error=NULL,screening_status='SCREENING_COMPLETE',screening_completed_at=?,updated_at=? WHERE profile_id=?", (json.dumps(payload, sort_keys=True), observed, observed, now(), profile_id))
    return analysis_id
