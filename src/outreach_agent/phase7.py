"""Resumable Phase 7 processing of the full saved OSM candidate pool.

Only an identity-verified, GEO-eligible company with a first-party public
business email or a form whose submission controls were observed counts as
contactable. Review approval and outreach authorization remain separate.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from outreach_agent.candidate_pool import ROOT, verify_candidate_batch
from outreach_agent.normalization import normalize_domain
from outreach_agent.phase6e_screening import PHASE6E_DB
from outreach_agent.phase6f import TOP5K
from outreach_agent.phase6g import contact_audit_rows
from outreach_agent.phase6h import PHASE6H_DB

PHASE6A_DB = ROOT / "data/phase6a_experimental.db"
REVIEW_DB = ROOT / "data/phase6g_contact_review.db"
PHASE7_DB = ROOT / "data/phase7_candidates.db"
OUT = ROOT / "data"
BASELINE_PATH = OUT / "phase7_baseline.json"
USABLE_CLASSES = {"PUBLIC_BUSINESS_EMAIL", "OFFICIAL_CONTACT_FORM"}
EMAIL_TYPES = {"GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "PARTNERSHIP_EMAIL"}
UNSUITABLE_MAILBOXES = {"support", "help", "billing", "accessibility", "privacy", "legal", "abuse", "compliance", "security", "press", "careers", "jobs", "hr", "webmaster", "no-reply", "noreply"}
RUN_TABLES = """
CREATE TABLE IF NOT EXISTS phase7_selection (
 candidate_id TEXT PRIMARY KEY, selection_rank INTEGER NOT NULL UNIQUE,
 selection_reason TEXT NOT NULL, selected_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase7_runs (
 run_id INTEGER PRIMARY KEY, batch_number INTEGER NOT NULL, chunk_number INTEGER NOT NULL,
 batch_size INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 attempted INTEGER NOT NULL, requests_attempted INTEGER NOT NULL, requests_succeeded INTEGER NOT NULL,
 requests_failed INTEGER NOT NULL, retries INTEGER NOT NULL, runtime_seconds REAL NOT NULL,
 request_budget INTEGER NOT NULL, time_budget_seconds REAL NOT NULL,
 counts_json TEXT NOT NULL, stop_reason TEXT
);
"""


def initialize(path: Path = PHASE7_DB, source: Path = PHASE6H_DB) -> None:
    """Clone the latest continuation once; never replace an existing Phase 7 checkpoint."""
    if not path.exists():
        if not source.exists():
            raise FileNotFoundError(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src, sqlite3.connect(path) as dst:
            src.backup(dst)
    with sqlite3.connect(path) as db:
        db.executescript(RUN_TABLES)
        db.commit()


def _screening_key(row: sqlite3.Row | dict[str, Any]) -> tuple[Any, ...]:
    """Reproducible high-yield ordering without changing qualification rules."""
    get = row.get if isinstance(row, dict) else row.__getitem__
    screen = get("screen_status") or "UNCERTAIN"
    chain = get("chain_location_concern") or ""
    domain = get("website_domain_concern") or ""
    return (
        screen != "LIKELY_FIT",
        chain != "NO_STRUCTURED_SIGNAL",
        domain != "NO_OBVIOUS_PATTERN",
        -int(get("priority_score") or 0),
        str(get("canonical_domain") or ""),
        str(get("candidate_id") or ""),
    )


def _has_operational_evidence(raw: str | None) -> bool:
    """Require a saved successful first-party fetch with substantive visible content."""
    try:
        evidence = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    pages = evidence.get("pages") or (evidence.get("saved_snapshot") or {}).get("pages") or []
    if not pages:
        snapshot = evidence.get("saved_snapshot") or evidence
        pages = [snapshot]
    return any(
        page.get("fetch_status") in {"SUCCESS", "PARTIAL"}
        and isinstance(page.get("http_status"), int) and 200 <= page["http_status"] < 400
        and int(page.get("visible_text_length") or 0) >= 80
        for page in pages if isinstance(page, dict)
    )


def _suitable_email(email: str) -> bool:
    """Exclude role inboxes whose stated purpose is not suitable for research invitations."""
    local = (email.split("@", 1)[0] if "@" in email else "").casefold().strip()
    return bool(local and local not in UNSUITABLE_MAILBOXES and not local.startswith(("no-reply", "noreply")))


def _confirmed_form_evidence(evidence: str) -> bool:
    """Require visible form/input/submission controls, not merely contact-page text."""
    lower = evidence.casefold()
    return any(token in lower for token in ("<form", "textarea", "submit", "form-submit", "form id", "button", "input", "your-message"))


def _cumulative_elapsed_seconds(prior_saved_runtime: float, current_invocation_wall_time: float) -> float:
    """Combine completed-run time with current wall time exactly once."""
    return max(0.0, float(prior_saved_runtime)) + max(0.0, float(current_invocation_wall_time))


def _union_run_wall_seconds(intervals: list[tuple[str, str]]) -> float:
    """Measure elapsed processing time once even if saved worker intervals overlap."""
    parsed = sorted((datetime.fromisoformat(start), datetime.fromisoformat(end)) for start, end in intervals)
    if not parsed:
        return 0.0
    total = 0.0
    current_start, current_end = parsed[0]
    for start, end in parsed[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    return total + (current_end - current_start).total_seconds()


def _run_lock_path(db_path: Path) -> Path:
    key = hashlib.sha256(str(db_path.resolve()).encode("utf-8")).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"geo-phase7-{key}.lock"


@contextmanager
def _exclusive_run_lock(db_path: Path):
    lock_path = _run_lock_path(db_path)
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("A Phase 7 run is already active for this database") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def select_remaining(*, db_path: Path = PHASE7_DB) -> int:
    """Snapshot every never-attempted candidate from the complete 57,664-row pool."""
    initialize(db_path)
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        existing = db.execute("SELECT COUNT(*) FROM phase7_selection").fetchone()[0]
        if existing:
            pending_unselected = db.execute("""SELECT COUNT(*) FROM candidates c LEFT JOIN phase7_selection s USING(candidate_id)
              WHERE c.verification_status='DISCOVERED_UNVERIFIED' AND s.candidate_id IS NULL""").fetchone()[0]
            if pending_unselected:
                raise ValueError(f"{pending_unselected} never-attempted candidates are absent from the persisted Phase 7 selection; refusing to silently skip them")
            return existing
        candidates = [dict(r) for r in db.execute("""SELECT c.candidate_id,c.canonical_domain,c.verification_status,
          s.screen_status,s.priority_score,s.chain_location_concern,s.website_domain_concern,s.category_group
          FROM candidates c LEFT JOIN phase6e_screening s USING(candidate_id)
          WHERE c.verification_status='DISCOVERED_UNVERIFIED'
          ORDER BY c.canonical_domain,c.candidate_id""")]
        candidates.sort(key=_screening_key)
        for rank, row in enumerate(candidates, 1):
            rationale = {
                "screen_status": row.get("screen_status") or "UNCERTAIN",
                "priority_score": row.get("priority_score") or 0,
                "chain_location_concern": row.get("chain_location_concern") or "",
                "website_domain_concern": row.get("website_domain_concern") or "",
                "category_group": row.get("category_group") or "",
            }
            db.execute("INSERT INTO phase7_selection VALUES(?,?,?,?)", (
                row["candidate_id"], rank, json.dumps(rationale, sort_keys=True), "full Phase 6E candidate pool"))
        db.commit()
        return len(candidates)


def _extract_channels(db_path: Path = PHASE7_DB) -> int:
    """Persist parser evidence only for verified + GEO-eligible selection records."""
    inserted = 0
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        candidates = db.execute("""SELECT candidate_id,canonical_domain,evidence_json FROM candidates
          WHERE verification_status='VERIFIED' AND geo_eligibility_status='ELIGIBLE'
          ORDER BY canonical_domain,candidate_id""").fetchall()
        for candidate in candidates:
            try:
                evidence = json.loads(candidate["evidence_json"] or "{}")
            except json.JSONDecodeError:
                continue
            pages = evidence.get("pages") or (evidence.get("saved_snapshot") or {}).get("pages") or []
            for page in pages:
                page_url = (page.get("final_url") or page.get("requested_url") or "").strip()
                if page_url:
                    path = page_url.split("?", 1)[0].casefold()
                    if any(term in path for term in ("contact", "partnership", "business-inquir", "get-in-touch")):
                        try:
                            if normalize_domain(page_url) == candidate["canonical_domain"]:
                                inserted += db.execute("""INSERT OR IGNORE INTO phase6f_contact_channels
                                  (candidate_id,channel_type,email,channel_url,source_url,evidence,purpose,review_status)
                                  VALUES(?,?,?,?,?,?,?,'PENDING')""", (
                                    candidate["candidate_id"], "CONTACT_PAGE", None, page_url, page_url,
                                    "Same-domain contact route fetched by existing bounded crawler; no submission method inferred",
                                    "Contact route only")).rowcount
                        except ValueError:
                            pass
                try:
                    channels = json.loads(page.get("extracted_channels") or "[]")
                except json.JSONDecodeError:
                    channels = []
                for channel in channels if isinstance(channels, list) else []:
                    kind = str(channel.get("type", "")).split(".")[-1]
                    email = (channel.get("email") or "").strip().casefold() or None
                    url = (channel.get("url") or "").strip()
                    source_url = (channel.get("source_url") or page_url).strip()
                    if kind not in EMAIL_TYPES | {"CONTACT_FORM"} or not url or not source_url:
                        continue
                    try:
                        domain = candidate["canonical_domain"]
                        if normalize_domain(source_url) != domain or normalize_domain(url) != domain:
                            continue
                        if email and ("@" not in email or normalize_domain("https://" + email.rsplit("@", 1)[-1]) != domain):
                            continue
                    except ValueError:
                        continue
                    inserted += db.execute("""INSERT OR IGNORE INTO phase6f_contact_channels
                      (candidate_id,channel_type,email,channel_url,source_url,evidence,purpose,review_status)
                      VALUES(?,?,?,?,?,?,?,'PENDING')""", (
                        candidate["candidate_id"], kind, email, url, source_url,
                        str(channel.get("evidence") or "")[:1200], str(channel.get("purpose") or "")[:500])).rowcount
        db.execute("UPDATE candidates SET contact_status='CONTACT_FOUND' WHERE candidate_id IN (SELECT candidate_id FROM phase6f_contact_channels)")
        db.commit()
    return inserted


def _pool_contact_rows(db_path: Path = PHASE7_DB, review_path: Path = REVIEW_DB) -> list[dict[str, Any]]:
    """Get strict deduped channels from the existing Phase 6G evidence auditor."""
    rows = contact_audit_rows(db_path, review_path)
    result = []
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        screened = {r[0]: r[1] for r in db.execute("SELECT candidate_id,chain_location_concern FROM phase6e_screening")}
        for row in rows:
            if row["channel_classification"] not in USABLE_CLASSES:
                continue
            if row["channel_classification"] == "PUBLIC_BUSINESS_EMAIL" and not _suitable_email(str(row.get("email") or "")):
                continue
            if row["channel_classification"] == "OFFICIAL_CONTACT_FORM" and not _confirmed_form_evidence(str(row.get("evidence") or "")):
                continue
            if row["verification_status"] != "VERIFIED" or row["geo_eligibility_status"] != "ELIGIBLE":
                continue
            if row["review_status"] == "REJECTED":
                continue
            try:
                candidate = db.execute("SELECT fetch_status,http_status,evidence_json FROM candidates WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
                live_page = _has_operational_evidence(candidate["evidence_json"] if candidate else None)
            except (TypeError, ValueError, json.JSONDecodeError):
                live_page = False
            if not live_page:
                continue
            # Possible chains/repeated-name records are review-only; avoid counting a location
            # as an independent company until an explicit identity review resolves it.
            chain = screened.get(row["candidate_id"], "NO_STRUCTURED_SIGNAL")
            if chain not in {"NO_STRUCTURED_SIGNAL", "PRESERVED_EXISTING_COHORT"}:
                continue
            result.append({**row, "contact_source": "phase6f/6h", "operational_evidence": "VERIFIED first-party website response with substantive visible text",
                           "identity_deduplication": "unique canonical domain; no unresolved chain/repeated-name flag"})
    return result


def _legacy_contact_rows(db_path: Path = PHASE6A_DB, candidate_db: Path = PHASE7_DB) -> list[dict[str, Any]]:
    """Strictly reconcile the active 537 cohort's preserved first-party contact evidence."""
    result: list[dict[str, Any]] = []
    with sqlite3.connect(f"file:{candidate_db.resolve()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        candidate_by_domain = {r["canonical_domain"]: dict(r) for r in source.execute(
            "SELECT candidate_id,canonical_domain,company_name,website,source_name,source_url,verification_status,geo_eligibility_status,fetch_status,http_status,evidence_json,qualification_evidence FROM candidates")}
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        query = """SELECT c.id,c.company_name,c.website,c.normalized_domain,c.industry,c.location,c.eligibility,
          c.geo_opportunity,c.qualification_evidence,c.review_status AS company_review_status,
          ct.id AS contact_id,ct.email,ct.channel_type,ct.channel_url,ct.source_url AS evidence_url,
          ct.evidence_text AS evidence,ct.purpose,ct.contact_type,ct.review_status,ct.validation_status,
          ct.updated_at,ct.discovered_at
          FROM companies c JOIN contacts ct ON ct.company_id=c.id ORDER BY c.normalized_domain,ct.id"""
        for raw in db.execute(query):
            row = dict(raw)
            domain = row["normalized_domain"]
            candidate = candidate_by_domain.get(domain)
            if not candidate or row["eligibility"] != "ELIGIBLE" or candidate["verification_status"] != "VERIFIED" or candidate["geo_eligibility_status"] != "ELIGIBLE":
                continue
            if row["review_status"] == "REJECTED" or row["company_review_status"] == "REJECTED":
                continue
            kind = (row.get("channel_type") or "").upper()
            email = (row.get("email") or "").strip().casefold()
            route = (row.get("channel_url") or "").strip()
            evidence_url = (row.get("evidence_url") or "").strip()
            try:
                if normalize_domain(evidence_url or route) != domain:
                    continue
                evidence = str(row.get("evidence") or "")
                if kind in EMAIL_TYPES:
                    if "@" not in email or normalize_domain("https://" + email.rsplit("@", 1)[-1]) != domain:
                        continue
                    if not _suitable_email(email) or row.get("contact_type") not in (None, "", "GENERIC_BUSINESS_CONTACT"):
                        continue
                    classification = "PUBLIC_BUSINESS_EMAIL"
                    channel = email
                elif kind == "CONTACT_FORM":
                    if not route or normalize_domain(route) != domain:
                        continue
                    if not _confirmed_form_evidence(evidence):
                        continue
                    classification = "OFFICIAL_CONTACT_FORM"
                    channel = route
                else:
                    continue
                live_page = _has_operational_evidence(candidate["evidence_json"])
                if not live_page:
                    continue
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            result.append({
                "candidate_id": candidate["candidate_id"], "company_name": row["company_name"], "website": row["website"],
                "canonical_domain": domain, "source_name": candidate["source_name"], "source_url": candidate["source_url"],
                "verification_status": candidate["verification_status"], "geo_eligibility_status": candidate["geo_eligibility_status"],
                "geo_opportunity_status": candidate.get("geo_opportunity_status", ""), "channel_classification": classification,
                "channel_type": kind, "email": email if classification == "PUBLIC_BUSINESS_EMAIL" else "", "channel_url": route or candidate["website"],
                "evidence_url": evidence_url, "evidence": evidence, "purpose": row.get("purpose") or "Public business contact evidence retained in the active company cohort",
                "review_status": row.get("review_status") or "PENDING", "confidence": row.get("confidence") or "UNKNOWN",
                "last_inspected_at": row.get("updated_at") or row.get("discovered_at") or "",
                "contact_source": "phase6a_experimental.db", "operational_evidence": "Verified website identity, GEO eligibility, and recent successful substantive website snapshot",
                "identity_deduplication": "unique canonical domain; matches existing active-cohort candidate",
            })
    return result


def qualified_channels(*, db_path: Path = PHASE7_DB, active_db: Path = PHASE6A_DB, review_path: Path = REVIEW_DB) -> list[dict[str, Any]]:
    """Current strict company-level eligibility as deduped channel evidence."""
    candidates = _pool_contact_rows(db_path, review_path) + _legacy_contact_rows(active_db, db_path)
    # Domain is the primary deduplication key; preserve multiple distinct first-party routes,
    # but emit a company's domains only once in the company export.
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in candidates:
        channel_identity = row.get("email") or row.get("channel_url") or ""
        key = (row["canonical_domain"], row["channel_classification"], str(channel_identity).rstrip("/").casefold())
        prior = unique.get(key)
        if prior is None or (prior.get("contact_source") != "phase6a_experimental.db" and row.get("contact_source") == "phase6a_experimental.db"):
            unique[key] = row
    return sorted(unique.values(), key=lambda r: (r["canonical_domain"], r["channel_classification"], r.get("email") or r.get("channel_url") or ""))


def _summary(db_path: Path = PHASE7_DB, active_db: Path = PHASE6A_DB, review_path: Path = REVIEW_DB) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        counts = Counter({r[0]: r[1] for r in db.execute("SELECT verification_status,COUNT(*) FROM candidates GROUP BY verification_status")})
        total = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        unique_domains = db.execute("SELECT COUNT(DISTINCT canonical_domain) FROM candidates").fetchone()[0]
        pool = db.execute("SELECT COUNT(*) FROM candidates WHERE source_name='OpenStreetMap (Geofabrik US Northeast extract)'").fetchone()[0]
        selected = db.execute("SELECT COUNT(*) FROM phase7_selection").fetchone()[0]
        processed_selected = db.execute("""SELECT COUNT(*) FROM candidates c JOIN phase7_selection s USING(candidate_id)
          WHERE c.verification_status<>'DISCOVERED_UNVERIFIED'""").fetchone()[0]
        run = db.execute("""SELECT COALESCE(SUM(attempted),0),COALESCE(SUM(requests_attempted),0),
          COALESCE(SUM(requests_succeeded),0),COALESCE(SUM(requests_failed),0),COALESCE(SUM(retries),0),
          COALESCE(SUM(runtime_seconds),0) FROM phase7_runs""").fetchone()
        run_intervals = db.execute("SELECT started_at,finished_at FROM phase7_runs").fetchall()
        source_counts = {r[0]: r[1] for r in db.execute("SELECT source_name,COUNT(*) FROM candidates GROUP BY source_name")}
    channels = qualified_channels(db_path=db_path, active_db=active_db, review_path=review_path)
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for row in channels:
        by_domain.setdefault(row["canonical_domain"], []).append(row)
    email = {domain for domain, rows in by_domain.items() if any(r["channel_classification"] == "PUBLIC_BUSINESS_EMAIL" for r in rows)}
    forms = {domain for domain, rows in by_domain.items() if any(r["channel_classification"] == "OFFICIAL_CONTACT_FORM" for r in rows)}
    return {
        "pool_records": total, "unique_canonical_domains": unique_domains, "osm_candidates": pool,
        "non_osm_existing_cohort_records": total - pool, "source_counts": source_counts,
        "verification_statuses": dict(sorted(counts.items())), "pending_unattempted": counts.get("DISCOVERED_UNVERIFIED", 0),
        "phase7_selected": selected, "phase7_processed": processed_selected,
        "http_attempts": run[1], "http_succeeded": run[2], "http_failed": run[3], "retries": run[4],
        "phase7_runtime_seconds": round(_union_run_wall_seconds(run_intervals), 2),
        "phase7_worker_seconds": round(run[5], 2), "contactable_companies": len(by_domain),
        "contactable_with_email": len(email), "contactable_with_confirmed_form": len(forms),
        "contactable_email_or_form": len(email | forms), "channels": len(channels),
    }


def preflight(*, db_path: Path = PHASE7_DB, source: Path = PHASE6H_DB) -> dict[str, Any]:
    initialize(db_path, source)
    # Phase 6E/6A saved snapshots may already contain first-party parser evidence.
    # Persisting that evidence to the Phase 7 copy is offline and idempotent.
    extracted_from_saved_evidence = _extract_channels(db_path)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        domains = db.execute("SELECT COUNT(*),COUNT(DISTINCT canonical_domain),SUM(canonical_domain IS NULL OR canonical_domain='') FROM candidates").fetchone()
        statuses = dict(db.execute("SELECT verification_status,COUNT(*) FROM candidates GROUP BY verification_status"))
        pending_osm = db.execute("""SELECT COUNT(*) FROM candidates WHERE source_name='OpenStreetMap (Geofabrik US Northeast extract)'
          AND verification_status='DISCOVERED_UNVERIFIED'""").fetchone()[0]
        runs = db.execute("SELECT COUNT(*) FROM phase7_runs").fetchone()[0]
    summary = _summary(db_path)
    return {"database": str(db_path), "source_database": str(source), "integrity": integrity,
            "candidate_rows": domains[0], "unique_canonical_domains": domains[1], "invalid_domains": domains[2],
            "verification_statuses": statuses, "unattempted_osm_candidates": pending_osm,
            "existing_phase7_run_chunks": runs, "strict_contactable_company_count": summary["contactable_companies"],
            "saved_contact_channels_recovered_offline": extracted_from_saved_evidence,
            "summary": summary}


def _run_unlocked(*, db_path: Path = PHASE7_DB, active_db: Path = PHASE6A_DB, review_path: Path = REVIEW_DB,
        batch_size: int = 250, request_cap: int = 5000, time_cap_seconds: int = 7200,
        chunk_request_budget: int = 500, request_timeout_seconds: float = 12,
        retry_count: int = 1, delay_between_requests_seconds: float = 1) -> dict[str, Any]:
    if not 1 <= batch_size <= 1000 or request_cap < 1 or time_cap_seconds < 1 or not 1 <= chunk_request_budget <= 500:
        raise ValueError("batch size must be 1..1000; request/time caps positive; chunk request budget 1..500")
    initialize(db_path)
    selected = select_remaining(db_path=db_path)
    before = _summary(db_path, active_db, review_path)
    processed_before = selected
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        processed_before -= db.execute("SELECT COUNT(*) FROM candidates WHERE verification_status='DISCOVERED_UNVERIFIED'").fetchone()[0]
        cumulative_requests = db.execute("SELECT COALESCE(SUM(requests_attempted),0) FROM phase7_runs").fetchone()[0]
        intervals = db.execute("SELECT started_at,finished_at FROM phase7_runs").fetchall()
        cumulative_runtime = _union_run_wall_seconds(intervals)
    started = time.monotonic()
    stop_reason = None
    batch_number = 0
    chunk_number = 0
    active_batch: int | None = None
    while True:
        elapsed = _cumulative_elapsed_seconds(cumulative_runtime, time.monotonic() - started)
        if cumulative_requests >= request_cap:
            stop_reason = "request_cap_exhausted"
            break
        if elapsed >= time_cap_seconds:
            stop_reason = "time_cap_exhausted"
            break
        with sqlite3.connect(db_path) as db:
            todo = [r[0] for r in db.execute("""SELECT c.candidate_id FROM candidates c
              JOIN phase7_selection s USING(candidate_id) WHERE c.verification_status='DISCOVERED_UNVERIFIED'
              ORDER BY s.selection_rank LIMIT 25""")]
            if not todo:
                break
            rank = db.execute("SELECT selection_rank FROM phase7_selection WHERE candidate_id=?", (todo[0],)).fetchone()[0]
        batch_number = (rank - 1) // batch_size + 1
        if active_batch != batch_number:
            active_batch, chunk_number = batch_number, 0
        chunk_number += 1
        remaining_time = max(1, time_cap_seconds - int(elapsed))
        budget = min(chunk_request_budget, request_cap - cumulative_requests)
        stamp = datetime.now(timezone.utc).isoformat()
        result = verify_candidate_batch(len(todo), candidate_db=db_path, candidate_ids=todo,
            request_budget=budget, time_budget_seconds=remaining_time, include_contact_evidence=True,
            request_timeout_seconds=request_timeout_seconds, retry_count=retry_count,
            delay_between_requests_seconds=delay_between_requests_seconds)
        _extract_channels(db_path)
        finished = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as db:
            db.execute("""INSERT INTO phase7_runs(batch_number,chunk_number,batch_size,started_at,finished_at,
              attempted,requests_attempted,requests_succeeded,requests_failed,retries,runtime_seconds,
              request_budget,time_budget_seconds,counts_json,stop_reason)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                batch_number, chunk_number, batch_size, stamp, finished, result["attempted"],
                result["requests_attempted"], result["requests_succeeded"], result["requests_failed"],
                result["retries"], result["runtime_seconds"], budget, remaining_time,
                json.dumps(result.get("counts", {}), sort_keys=True), None))
            db.commit()
        cumulative_requests += result["requests_attempted"]
        # Current invocation wall time is measured from `started` on the next loop.
        # Do not also add this chunk's collector time or the cumulative cap is double-counted.
        if result.get("time_budget_exhausted"):
            stop_reason = "time_cap_exhausted"
            break
        if result.get("request_budget_exhausted") and result["attempted"] == 0:
            stop_reason = "request_cap_exhausted"
            break
        if result["attempted"] == 0:
            stop_reason = "no_progress"
            break
        # Evaluate completed configured cohort intervals; pause if either upstream quality
        # dimension falls below half the previously measured Phase 6F baseline.
        interval_start = (batch_number - 1) * batch_size + 1
        interval_end = batch_number * batch_size
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
            records = db.execute("""SELECT c.verification_status,c.geo_eligibility_status FROM candidates c
              JOIN phase7_selection s USING(candidate_id) WHERE s.selection_rank BETWEEN ? AND ?""",
              (interval_start, interval_end)).fetchall()
            expected = min(batch_size, selected - interval_start + 1)
        if expected > 0 and len(records) >= expected and all(r[0] != "DISCOVERED_UNVERIFIED" for r in records):
            verified_rate = sum(r[0] == "VERIFIED" for r in records) / expected
            eligible_rate = sum(r[1] == "ELIGIBLE" for r in records) / expected
            if verified_rate < 0.23 or eligible_rate < 0.204:
                stop_reason = "quality_degradation_pause"
                break
        if result.get("request_budget_exhausted"):
            stop_reason = "request_cap_exhausted"
            break
    _extract_channels(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE phase7_runs SET stop_reason=? WHERE run_id=(SELECT MAX(run_id) FROM phase7_runs)",
                   (stop_reason or "selection_complete",))
        db.commit()
    export_outputs(db_path=db_path, active_db=active_db, review_path=review_path)
    after = _summary(db_path, active_db, review_path)
    after.update({"selected_workload": selected, "processed_this_invocation": after["phase7_processed"] - processed_before,
                  "runtime_seconds_this_invocation": round(time.monotonic() - started, 2),
                  "contactable_added_this_invocation": max(0, after["contactable_companies"] - before["contactable_companies"]),
                  "stop_reason": stop_reason or "selection_complete", "integrity": integrity(db_path)})
    return after


def run(*, db_path: Path = PHASE7_DB, active_db: Path = PHASE6A_DB, review_path: Path = REVIEW_DB,
        batch_size: int = 250, request_cap: int = 5000, time_cap_seconds: int = 7200,
        chunk_request_budget: int = 500, request_timeout_seconds: float = 12,
        retry_count: int = 1, delay_between_requests_seconds: float = 1) -> dict[str, Any]:
    """Run with an OS lock so concurrent resumes cannot duplicate crawl work."""
    with _exclusive_run_lock(db_path):
        return _run_unlocked(db_path=db_path, active_db=active_db, review_path=review_path,
            batch_size=batch_size, request_cap=request_cap, time_cap_seconds=time_cap_seconds,
            chunk_request_budget=chunk_request_budget, request_timeout_seconds=request_timeout_seconds,
            retry_count=retry_count, delay_between_requests_seconds=delay_between_requests_seconds)


def integrity(path: Path = PHASE7_DB) -> str:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def _metrics(db_path: Path = PHASE7_DB, *, batch_size: int = 500) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        selected = db.execute("SELECT COUNT(*) FROM phase7_selection").fetchone()[0]
        candidates = [dict(r) for r in db.execute("""SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status,
          s.selection_rank FROM candidates c JOIN phase7_selection s USING(candidate_id) ORDER BY s.selection_rank""")]
        runs = [dict(r) for r in db.execute("SELECT * FROM phase7_runs ORDER BY run_id")]
    contacts = qualified_channels(db_path=db_path)
    by_candidate: dict[str, set[str]] = {}
    for row in contacts:
        by_candidate.setdefault(row["candidate_id"], set()).add(row["channel_classification"])
    outputs = []
    for start in range(1, selected + 1, batch_size):
        end = min(selected, start + batch_size - 1)
        items = [r for r in candidates if start <= r["selection_rank"] <= end]
        processed = [r for r in items if r["verification_status"] != "DISCOVERED_UNVERIFIED"]
        emails = {r["candidate_id"] for r in processed if "PUBLIC_BUSINESS_EMAIL" in by_candidate.get(r["candidate_id"], set())}
        forms = {r["candidate_id"] for r in processed if "OFFICIAL_CONTACT_FORM" in by_candidate.get(r["candidate_id"], set())}
        ran = [r for r in runs if (r["batch_number"] - 1) * r["batch_size"] + 1 <= end and r["batch_number"] * r["batch_size"] >= start]
        outputs.append({
            "selection_rank_start": start, "selection_rank_end": end, "selected": len(items), "processed": len(processed),
            "verified": sum(r["verification_status"] == "VERIFIED" for r in processed),
            "identity_verification_rate": round(sum(r["verification_status"] == "VERIFIED" for r in processed) / len(processed), 4) if processed else "",
            "geo_eligible": sum(r["geo_eligibility_status"] == "ELIGIBLE" for r in processed),
            "geo_eligibility_rate": round(sum(r["geo_eligibility_status"] == "ELIGIBLE" for r in processed) / len(processed), 4) if processed else "",
            "business_email_companies": len(emails), "confirmed_form_companies": len(forms),
            "unique_contactable_companies": len(emails | forms),
            "http_attempts": sum(r["requests_attempted"] for r in ran), "http_successes": sum(r["requests_succeeded"] for r in ran),
            "http_failures": sum(r["requests_failed"] for r in ran), "retries": sum(r["retries"] for r in ran),
            "runtime_seconds": round(sum(r["runtime_seconds"] for r in ran), 2),
            "http_attempts_per_contactable_company": round(sum(r["requests_attempted"] for r in ran) / len(emails | forms), 2) if emails | forms else "",
            "seconds_per_contactable_company": round(sum(r["runtime_seconds"] for r in ran) / len(emails | forms), 2) if emails | forms else "",
        })
    return outputs


def export_outputs(*, db_path: Path = PHASE7_DB, active_db: Path = PHASE6A_DB,
                   review_path: Path = REVIEW_DB, output_dir: Path = OUT) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    channels = qualified_channels(db_path=db_path, active_db=active_db, review_path=review_path)
    companies: dict[str, dict[str, Any]] = {}
    for row in channels:
        domain = row["canonical_domain"]
        company = companies.setdefault(domain, {key: row.get(key, "") for key in (
            "candidate_id", "company_name", "website", "canonical_domain", "source_name", "source_url",
            "verification_status", "geo_eligibility_status", "geo_opportunity_status", "last_inspected_at",
            "operational_evidence", "identity_deduplication")})
        company["contact_methods"] = sorted(set(company.get("contact_methods", []) + [row["channel_classification"]]))
        company["contact_evidence_urls"] = sorted(set(company.get("contact_evidence_urls", []) + [row.get("evidence_url") or row.get("channel_url") or ""]))
        company["emails"] = sorted(set(company.get("emails", []) + ([row["email"]] if row.get("email") else [])))
        company["confirmed_form_urls"] = sorted(set(company.get("confirmed_form_urls", []) + ([row["channel_url"]] if row["channel_classification"] == "OFFICIAL_CONTACT_FORM" else [])))
        company["review_status"] = "PENDING" if row.get("review_status") != "APPROVED" else "APPROVED_CONTACT_ONLY"
        company["outreach_authorization"] = "NOT_GRANTED"
    company_rows = [{**row, "contact_methods": ";".join(row.get("contact_methods", [])),
                     "contact_evidence_urls": ";".join(row.get("contact_evidence_urls", [])),
                     "emails": ";".join(row.get("emails", [])), "confirmed_form_urls": ";".join(row.get("confirmed_form_urls", []))}
                    for _, row in sorted(companies.items())]
    company_fields = ["candidate_id", "company_name", "website", "canonical_domain", "source_name", "source_url",
                      "verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_methods",
                      "emails", "confirmed_form_urls", "contact_evidence_urls", "operational_evidence",
                      "identity_deduplication", "last_inspected_at", "review_status", "outreach_authorization"]
    channel_fields = ["candidate_id", "company_name", "website", "canonical_domain", "source_name", "source_url",
                      "verification_status", "geo_eligibility_status", "geo_opportunity_status", "channel_classification",
                      "channel_type", "email", "channel_url", "evidence_url", "evidence", "purpose", "confidence",
                      "review_status", "last_inspected_at", "contact_source", "operational_evidence", "identity_deduplication"]

    def write(filename: str, rows: list[dict[str, Any]], fields: list[str]) -> str:
        path = output_dir / filename
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                values = {}
                for field in fields:
                    value = row.get(field, "")
                    if isinstance(value, str) and value[:1] in "=+-@":
                        value = "'" + value
                    values[field] = value
                writer.writerow(values)
        return str(path)

    summary = _summary(db_path, active_db, review_path)
    baseline = {}
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        baseline = {"strict_contactable_companies": summary["contactable_companies"],
                    "candidate_records": summary["pool_records"], "unique_canonical_domains": summary["unique_canonical_domains"]}
    exclusions = []
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        records = db.execute("""SELECT c.candidate_id,c.company_name,c.canonical_domain,c.source_name,c.source_url,
          c.verification_status,c.geo_eligibility_status,c.contact_status,c.verification_reason,c.last_inspected_at,
          s.screen_status,s.chain_location_concern,s.website_domain_concern FROM candidates c
          LEFT JOIN phase6e_screening s USING(candidate_id) ORDER BY c.canonical_domain,c.candidate_id""").fetchall()
        selection_count = db.execute("SELECT COUNT(*) FROM phase7_selection").fetchone()[0]
        phase7_runs = [dict(r) for r in db.execute("SELECT * FROM phase7_runs ORDER BY run_id")]
        processed_failures = [dict(r) for r in db.execute("""SELECT c.candidate_id,c.company_name,c.canonical_domain,c.website,
          c.source_name,c.source_url,c.verification_status,c.fetch_status,c.http_status,c.verification_reason,
          c.last_inspected_at FROM candidates c JOIN phase7_selection s USING(candidate_id)
          WHERE c.verification_status NOT IN ('VERIFIED','DISCOVERED_UNVERIFIED') ORDER BY s.selection_rank""")]
    for record in records:
        row = dict(record)
        if row["canonical_domain"] in companies:
            continue
        reasons = []
        if row["verification_status"] != "VERIFIED": reasons.append("IDENTITY_NOT_VERIFIED")
        if row["geo_eligibility_status"] != "ELIGIBLE": reasons.append("GEO_NOT_ELIGIBLE")
        if row["chain_location_concern"] not in (None, "NO_STRUCTURED_SIGNAL", "PRESERVED_EXISTING_COHORT"):
            reasons.append("IDENTITY_DEDUP_REVIEW_REQUIRED")
        if not reasons: reasons.append("NO_FIRST_PARTY_EMAIL_OR_CONFIRMED_FORM")
        exclusions.append({**row, "exclusion_reasons": ";".join(reasons)})
    metrics = _metrics(db_path)
    from outreach_agent.phase6g import failure_category
    failed_exports = []
    failure_distribution: Counter[str] = Counter()
    for row in processed_failures:
        category, next_action = failure_category(row["verification_status"], row.get("fetch_status") or "",
                                                  row.get("http_status"), row.get("verification_reason") or "")
        failure_distribution[category] += 1
        failed_exports.append({**row, "failure_category": category, "next_safe_action": next_action})
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        total_requests, total_success, total_failed, total_retries, worker_runtime = db.execute(
            "SELECT COALESCE(SUM(requests_attempted),0),COALESCE(SUM(requests_succeeded),0),COALESCE(SUM(requests_failed),0),COALESCE(SUM(retries),0),COALESCE(SUM(runtime_seconds),0) FROM phase7_runs").fetchone()
        run_intervals = db.execute("SELECT started_at,finished_at FROM phase7_runs").fetchall()
        runtime = _union_run_wall_seconds(run_intervals)
        status_counts = dict(db.execute("SELECT verification_status,COUNT(*) FROM candidates GROUP BY verification_status"))
        candidate_total = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        pending = status_counts.get("DISCOVERED_UNVERIFIED", 0)
    report = ["# Phase 7: Contactable GEO Research Companies", "",
      "## Strict baseline and current result", "",
      f"- Full existing candidate pool: {candidate_total:,} distinct canonical domains; source composition: `{json.dumps(summary['source_counts'], sort_keys=True)}`.",
      f"- Strict baseline from the first export before offline evidence recovery: {baseline.get('strict_contactable_companies', 0):,} distinct companies.",
      f"- Reconciled strict starting baseline after recovery of existing Phase 6 saved contact evidence: {baseline.get('strict_contactable_companies_after_offline_recovery', baseline.get('strict_contactable_companies', 0)):,} distinct companies.",
      f"- Phase 7 candidates snapshotted for first-pass processing: {selection_count:,}.",
      f"- Strict contactable-company count now: {summary['contactable_companies']:,} (verified identity + existing GEO eligibility + operational first-party website evidence + genuine same-domain business email or confirmed form).",
      f"- With public business email: {summary['contactable_with_email']:,}; with confirmed form: {summary['contactable_with_confirmed_form']:,}; distinct union: {summary['contactable_email_or_form']:,}.",
      f"- Remaining unattempted candidates: {pending:,}; target shortfall: {max(0,5000-summary['contactable_companies']):,}.", "",
      "## Processing and yield", "",
      f"- Unique Phase 7 candidates processed: {summary['phase7_processed']:,}; HTTP attempts: {total_requests:,} (successful {total_success:,}, failed {total_failed:,}, retries {total_retries:,}).",
      f"- Union wall time across persisted run intervals: {runtime:.1f}s; summed worker runtime (can double-count overlapping workers): {worker_runtime:.1f}s.",
      "- Some workers overlapped before a single-worker lock was added; HTTP attempts are actual request-attempt counts, but per-window request/runtime efficiency includes duplicate work during those intervals.",
      f"- Website verification outcomes across pool: `{json.dumps(status_counts, sort_keys=True)}`.",
      f"- Phase 7 unresolved/failure categories among processed candidates: `{json.dumps(dict(sorted(failure_distribution.items())), sort_keys=True)}`.",
      "- Yield metrics are reported in 500-candidate selection-rank windows. Rates use processed records; they are observations, not population guarantees.",
      "- Channel status and outreach authorization are separate; all unreviewed contact channels remain pending and every company export says outreach authorization NOT_GRANTED.", "",
      "## Evidence, limits, and source scope", "",
      "The existing Phase 6E pool contains 57,664 distinct domains, including the full 537-company active cohort. No new source was acquired because the existing pool is not exhausted. Website requests use the existing identity verifier, GEO qualification rules, bounded request/time/retry controls, robots/access handling, and saved evidence cache. Blocked, robots-denied, failed, ambiguous, and identity-mismatched sites are not bypassed or promoted. Contact pages without a discovered email or confirmed form do not count.",
      "The ‘operational’ indicator is bounded evidence: a current verified first-party website fetch with a successful 2xx/3xx response and substantive visible text. It does not independently establish business registration or revenue. Companies flagged for possible chain/franchise or repeated-name identity review are excluded from the automated total until resolved.", "",
      "## Resume", "", f"Continue with: `PYTHONPATH=src uv run python scripts/phase7_contacts.py run --batch-size 500 --request-cap {max(10000, int(total_requests) + 5000)} --time-cap-seconds {max(21600, int(runtime) + 10800)} --chunk-request-budget 500`.",
      "The request and time caps are cumulative across saved Phase 7 runs; saved wall intervals are unioned to avoid double-counting overlap. Raise the caps to allow additional work. No email or form submissions are performed.", "",
      "## Outputs", "", "- `data/phase7_contactable_companies.csv`", "- `data/phase7_contact_channel_audit.csv`", "- `data/phase7_validation_failures.csv`", "- `data/phase7_exclusion_queue.csv`", "- `data/phase7_yield_metrics.csv`", "- `data/phase7_candidates.db`", ""]
    report_text = "\n".join(report)
    result = {
        "companies": write("phase7_contactable_companies.csv", company_rows, company_fields),
        "channels": write("phase7_contact_channel_audit.csv", channels, channel_fields),
        "validation_failures": write("phase7_validation_failures.csv", failed_exports, ["candidate_id", "company_name", "canonical_domain", "website", "source_name", "source_url", "verification_status", "fetch_status", "http_status", "failure_category", "verification_reason", "last_inspected_at", "next_safe_action"]),
        "exclusions": write("phase7_exclusion_queue.csv", exclusions, ["candidate_id", "company_name", "canonical_domain", "source_name", "source_url", "verification_status", "geo_eligibility_status", "contact_status", "screen_status", "chain_location_concern", "website_domain_concern", "verification_reason", "last_inspected_at", "exclusion_reasons"]),
        "yield_metrics": write("phase7_yield_metrics.csv", metrics, list(metrics[0].keys()) if metrics else ["selection_rank_start"]),
    }
    (output_dir / "phase7_final_report.md").write_text(report_text, encoding="utf-8")
    result["report"] = str(output_dir / "phase7_final_report.md")
    return result
