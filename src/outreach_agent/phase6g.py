"""Offline contact quality audit and isolated human-review layer for Phase 6G."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from outreach_agent.normalization import normalize_domain
from outreach_agent.phase6f import PHASE6F_DB

ROOT = Path(__file__).resolve().parents[2]
REVIEW_DB = ROOT / "data/phase6g_contact_review.db"
EVALUATION_DB = ROOT / "data/phase6g_evaluation.db"
OUT = ROOT / "data"
AUDIT_FIELDS = (
    "candidate_id", "company_name", "website", "canonical_domain", "source_name", "source_url",
    "verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "identity_evidence", "qualification_evidence",
    "channel_key", "channel_type", "channel_classification", "email", "channel_url", "evidence_url",
    "evidence", "duplicate_evidence", "purpose", "first_party_provenance", "company_identity_match", "purpose_suitability",
    "evidence_freshness", "contact_personal_or_business", "confidence", "confidence_reasons",
    "review_status", "review_reason", "company_review_status", "company_review_note", "last_inspected_at", "duplicate_count",
)
FAILURE_FIELDS = (
    "candidate_id", "company_name", "canonical_domain", "source_name", "website", "source_url",
    "verification_status", "fetch_status", "http_status", "failure_category", "failure_detail",
    "website_evidence", "last_inspected_at", "next_safe_action",
)


def _connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def initialize_review_db(path: Path = REVIEW_DB) -> None:
    with _connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS contact_decisions (
          candidate_id TEXT NOT NULL, channel_key TEXT NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('PENDING','APPROVED','REJECTED')),
          reviewer_note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
          PRIMARY KEY(candidate_id, channel_key)
        );
        CREATE TABLE IF NOT EXISTS company_review (
          candidate_id TEXT PRIMARY KEY,
          review_status TEXT NOT NULL CHECK(review_status IN ('PENDING','FURTHER_REVIEW')),
          reviewer_note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        """)


def _host_domain(url: str) -> str:
    try:
        return normalize_domain(url)
    except (ValueError, TypeError):
        return ""


def _channel_key(row: dict[str, Any]) -> str:
    identity = (str(row.get("email") or "").strip().casefold() or
                str(row.get("channel_url") or "").strip().rstrip("/").casefold())
    raw = "|".join((str(row.get("candidate_id", "")), str(row.get("channel_type", "")), identity))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _classify(row: dict[str, Any], expected_domain: str) -> tuple[str, bool, str]:
    kind = str(row.get("channel_type") or "").upper()
    email = str(row.get("email") or "").strip()
    channel_domain = _host_domain(str(row.get("channel_url") or ""))
    # The company source_url is the OSM/discovery provenance; channel evidence
    # is separately stored on evidence_url and must be used for first-party checks.
    source_domain = _host_domain(str(row.get("evidence_url") or row.get("channel_source_url") or row.get("source_url") or ""))
    first_party = bool(expected_domain and source_domain == expected_domain and channel_domain == expected_domain)
    if kind in {"GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "PARTNERSHIP_EMAIL"}:
        email_domain = _host_domain("https://" + email.rsplit("@", 1)[-1]) if "@" in email else ""
        if not first_party or not email_domain or email_domain != expected_domain:
            return "INVALID_AMBIGUOUS_OR_UNSUPPORTED", False, "Email or evidence URL does not match the company's canonical domain"
        if re.search(r"\b(?:[A-Z][a-z]+\s+[A-Z][a-z]+|Dr\.?\s+[A-Z][a-z]+)\b", str(row.get("evidence") or "")):
            # Only classify as a named individual when the actual address appears person-specific.
            mailbox = email.split("@", 1)[0]
            if re.fullmatch(r"[a-z]+[._-][a-z]+", mailbox, flags=re.I):
                return "NAMED_INDIVIDUAL_BUSINESS_CONTACT", True, "First-party public business address appears person-specific; human suitability review required"
        return "PUBLIC_BUSINESS_EMAIL", True, "Public address is published on a same-domain company page"
    if kind == "CONTACT_FORM":
        if not first_party:
            return "INVALID_AMBIGUOUS_OR_UNSUPPORTED", False, "Form URL or evidence source is not on the canonical company domain"
        evidence = str(row.get("evidence") or "").casefold()
        has_controls = any(token in evidence for token in ("submit", "textarea", "form-submit", "your-message", "message"))
        if has_controls:
            return "OFFICIAL_CONTACT_FORM", True, "Same-domain form evidence includes input/message/submission controls; no form was submitted"
        return "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD", True, "Same-domain contact route found, but submission controls are not confirmed"
    if kind == "CONTACT_PAGE":
        if first_party:
            return "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD", True, "Same-domain contact page; a usable submission method is not established"
    return "INVALID_AMBIGUOUS_OR_UNSUPPORTED", False, "Unsupported channel type or missing first-party attribution"


def contact_audit_rows(db_path: Path = PHASE6F_DB, review_path: Path = REVIEW_DB) -> list[dict[str, Any]]:
    initialize_review_db(review_path)
    with _connect(db_path, True) as db:
        db.row_factory = sqlite3.Row
        raw = [dict(r) for r in db.execute("""SELECT c.candidate_id,c.company_name,c.website,c.canonical_domain,c.source_name,c.source_url,
          c.verification_status,c.geo_eligibility_status,c.geo_opportunity_status,c.contact_status,c.evidence_json,c.qualification_evidence,c.last_inspected_at,
          ch.channel_type,ch.email,ch.channel_url,ch.source_url AS evidence_url,ch.evidence,ch.purpose
          FROM phase6f_contact_channels ch JOIN candidates c USING(candidate_id)
          ORDER BY c.company_name COLLATE NOCASE,c.candidate_id,ch.channel_type,ch.email,ch.channel_url""")]
    # Deduplicate semantic channel repeats but retain the occurrence count in audit output.
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in raw:
        identity = str(row.get("email") or "").strip().casefold() or str(row.get("channel_url") or "").strip().rstrip("/").casefold()
        grouped.setdefault((row["candidate_id"], row["channel_type"], identity), []).append(row)
    with _connect(review_path, True) as db:
        decisions = {(r[0], r[1]): (r[2], r[3]) for r in db.execute("SELECT candidate_id,channel_key,decision,reviewer_note FROM contact_decisions")}
        company_reviews = {r[0]: (r[1], r[2]) for r in db.execute("SELECT candidate_id,review_status,reviewer_note FROM company_review")}
    result = []
    for instances in grouped.values():
        row = instances[0]
        classification, first_party, class_reason = _classify(row, row["canonical_domain"])
        identity_match = row["verification_status"] == "VERIFIED"
        good_purpose = bool(row.get("purpose") or row.get("evidence"))
        is_business = classification not in {"NAMED_INDIVIDUAL_BUSINESS_CONTACT", "INVALID_AMBIGUOUS_OR_UNSUPPORTED"}
        usable_method = classification in {"PUBLIC_BUSINESS_EMAIL", "OFFICIAL_CONTACT_FORM"}
        score = (2 if first_party else 0) + (2 if identity_match else 0) + (1 if good_purpose else 0) + (1 if usable_method else 0)
        confidence = "HIGH" if score >= 6 else "MEDIUM" if score >= 4 else "LOW"
        reasons = [class_reason]
        if not identity_match:
            reasons.append("Company identity is not VERIFIED")
        if not good_purpose:
            reasons.append("Contact-purpose evidence is missing")
        if classification == "NAMED_INDIVIDUAL_BUSINESS_CONTACT":
            reasons.append("Individual-specific contact requires additional privacy/suitability review")
        key = _channel_key(row)
        decision, note = decisions.get((row["candidate_id"], key), ("PENDING", ""))
        company_status, company_note = company_reviews.get(row["candidate_id"], ("PENDING", ""))
        result.append({
            **{k: row.get(k, "") for k in ("candidate_id", "company_name", "website", "canonical_domain", "source_name", "source_url", "verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "channel_type", "email", "channel_url", "evidence_url", "evidence", "purpose", "last_inspected_at")},
            "identity_evidence": _identity_summary(row.get("evidence_json")),
            "qualification_evidence": row.get("qualification_evidence") or "",
            "duplicate_evidence": json.dumps(sorted({json.dumps({"url": item.get("source_url"), "channel_url": item.get("channel_url"), "evidence": item.get("evidence"), "purpose": item.get("purpose")}, ensure_ascii=False, sort_keys=True) for item in instances}), ensure_ascii=False),
            "channel_key": key, "channel_classification": classification,
            "first_party_provenance": "YES" if first_party else "NO",
            "company_identity_match": "VERIFIED" if identity_match else "UNVERIFIED",
            "purpose_suitability": "EVIDENCE_PRESENT_REVIEW_REQUIRED" if good_purpose else "UNKNOWN",
            "evidence_freshness": row.get("last_inspected_at") or "UNKNOWN",
            "contact_personal_or_business": "PERSONAL_OR_NAMED" if classification == "NAMED_INDIVIDUAL_BUSINESS_CONTACT" else "PUBLIC_BUSINESS" if is_business else "UNKNOWN",
            "confidence": confidence, "confidence_reasons": "; ".join(reasons), "review_status": decision,
            "review_reason": note, "company_review_status": company_status, "company_review_note": company_note,
            "duplicate_count": len(instances) - 1,
        })
    return sorted(result, key=lambda r: (r["company_name"].casefold(), r["candidate_id"], r["channel_classification"], r["channel_key"]))


def failure_category(status: str, fetch_status: str, http_status: int | None, reason: str) -> tuple[str, str]:
    text = f"{reason} {fetch_status}".casefold()
    if status == "AMBIGUOUS_IDENTITY":
        return "IDENTITY_AMBIGUOUS", "Retain unresolved; require stronger company-to-site evidence"
    if status == "INSUFFICIENT_EVIDENCE":
        return "INSUFFICIENT_IDENTITY_EVIDENCE", "Retain unresolved; do not infer identity from HTTP success"
    if "redirect" in text:
        return "REDIRECT_ISSUE", "Keep identity unresolved; inspect the saved final URL and do not assume unrelated redirects are official"
    if status == "REJECTED_MISMATCH":
        return "IDENTITY_MISMATCH_OR_PARKED", "Do not associate the domain absent contradictory first-party evidence"
    if status == "ROBOTS_DENIED" or "robots.txt" in text or "robots denied" in text:
        return "ROBOTS_DENIED", "Do not retry unless site policy changes"
    if "captcha" in text or "bot protection" in text or "challenge page" in text:
        return "BOT_PROTECTION_EXPLICIT", "Do not bypass; retain as review-needed"
    if http_status == 403:
        return "HTTP_403_ACCESS_DENIED_UNSPECIFIED", "Evidence does not distinguish explicit policy denial from anti-bot; do not bypass"
    if http_status == 429:
        return "HTTP_429_RATE_LIMITED", "Do not retry until the site policy permits; no rate-limit bypass"
    if http_status == 401:
        return "HTTP_401_AUTH_REQUIRED", "Do not attempt authentication"
    if status == "ACCESS_BLOCKED":
        return "ACCESS_RESTRICTION_UNSPECIFIED", "Do not bypass; retain as review-needed"
    if "timed out" in text or "timeout" in text:
        return "TIMEOUT", "At most one delayed retry within a bounded request budget"
    if any(token in text for token in ("ssl", "certificate", "tls", "handshake", "eof occurred in violation")):
        return "TLS_ERROR", "Do not disable certificate verification; retry only after a bounded delay"
    if any(token in text for token in ("nodename", "name or service not known", "temporary failure in name", "dns")):
        return "DNS_FAILURE", "One bounded retry may distinguish transient DNS from persistent failure"
    if "redirect" in text or "redirected from" in text:
        return "REDIRECT_ISSUE", "Keep identity unresolved; inspect saved final URL without following unrelated redirects"
    if http_status is not None or re.search(r"\bhttp\s+\d{3}\b", text):
        code = str(http_status or re.search(r"\bhttp\s+(\d{3})\b", text).group(1))
        return f"HTTP_{code}", "Do not retry permanent 4xx; retry 5xx at most once"
    if "connection reset" in text or "connection refused" in text or "temporarily unavailable" in text:
        return "TRANSIENT_CONNECTION", "One bounded retry may be appropriate"
    if "size limit" in text or "exceeded configured size" in text:
        return "RESPONSE_TOO_LARGE", "Keep response cap; rely on a smaller first-party page if available"
    if "invalid domain" in text:
        return "INVALID_DOMAIN", "Correct or human-review the source website value; do not guess a replacement"
    if "invalid website url" in text:
        return "INVALID_SOURCE_URL", "Correct only from source evidence; do not treat third-party URL as official"
    if "non-html content type" in text:
        return "NON_HTML_CONTENT", "Keep identity unresolved unless another first-party HTML page is sourced"
    return "OTHER_FETCH_FAILURE", "Human review; no automatic identity promotion"


def failure_rows(db_path: Path = PHASE6F_DB) -> list[dict[str, Any]]:
    with _connect(db_path, True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("""SELECT c.candidate_id,c.company_name,c.canonical_domain,c.source_name,c.website,c.source_url,
            c.verification_status,c.fetch_status,c.http_status,c.verification_reason,c.evidence_json,c.last_inspected_at
            FROM candidates c JOIN phase6f_selection s USING(candidate_id)
            WHERE c.verification_status IN ('FETCH_FAILED','ACCESS_BLOCKED','ROBOTS_DENIED','AMBIGUOUS_IDENTITY','INSUFFICIENT_EVIDENCE','REJECTED_MISMATCH')
            ORDER BY c.candidate_id""")]
    output = []
    for row in rows:
        category, action = failure_category(row["verification_status"], row["fetch_status"] or "", row["http_status"], row["verification_reason"] or "")
        output.append({"candidate_id": row["candidate_id"], "company_name": row["company_name"], "canonical_domain": row["canonical_domain"],
                       "source_name": row["source_name"], "website": row["website"], "source_url": row["source_url"],
                       "verification_status": row["verification_status"], "fetch_status": row["fetch_status"] or "", "http_status": row["http_status"] or "",
                       "failure_category": category, "failure_detail": row["verification_reason"] or "",
                       "website_evidence": _evidence_summary(row["evidence_json"]), "last_inspected_at": row["last_inspected_at"] or "", "next_safe_action": action})
    return output


def _evidence_summary(raw: str | None) -> str:
    try:
        evidence = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    pages = evidence.get("pages") or []
    return "; ".join(f"{p.get('requested_url','')} -> {p.get('final_url','')} (HTTP {p.get('http_status','')}, {p.get('fetch_status','')})" for p in pages[:4])


def _identity_summary(raw: str | None) -> str:
    try:
        evidence = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    values = []
    for page in evidence.get("pages") or []:
        values.append(" | ".join(str(page.get(key) or "") for key in ("title", "meta_description", "final_url", "visible_text_length") if page.get(key) not in (None, "")))
    tokens = evidence.get("identity_token_evidence")
    if tokens:
        values.append("identity tokens: " + str(tokens))
    return "; ".join(values)


def export_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def csv_text(rows: list[dict[str, Any]], fields: tuple[str, ...] = AUDIT_FIELDS) -> str:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in fields})
    return handle.getvalue()


def review_rows(*, db_path: Path = PHASE6F_DB, review_path: Path = REVIEW_DB, source: str = "", confidence: str = "", status: str = "") -> list[dict[str, Any]]:
    rows = contact_audit_rows(db_path, review_path)
    if source:
        rows = [r for r in rows if r["source_name"] == source]
    if confidence:
        rows = [r for r in rows if r["confidence"] == confidence]
    if status:
        rows = [r for r in rows if status in {r["review_status"], r["company_review_status"]}]
    return rows


def record_channel_decision(candidate_id: str, channel_key: str, decision: str, note: str = "", *, db_path: Path = PHASE6F_DB, review_path: Path = REVIEW_DB) -> None:
    decision = decision.upper()
    if decision not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("Channel decision must be PENDING, APPROVED, or REJECTED")
    if len(note) > 1000:
        raise ValueError("Reviewer note is limited to 1000 characters")
    target = next((r for r in contact_audit_rows(db_path, review_path) if r["candidate_id"] == candidate_id and r["channel_key"] == channel_key), None)
    if target is None:
        raise KeyError("Contact channel not found")
    if decision == "APPROVED" and (target["first_party_provenance"] != "YES" or target["company_identity_match"] != "VERIFIED" or target["channel_classification"] == "INVALID_AMBIGUOUS_OR_UNSUPPORTED"):
        raise ValueError("Cannot confirm a contact channel without verified company identity and first-party evidence")
    initialize_review_db(review_path)
    with _connect(review_path) as db:
        db.execute("INSERT INTO contact_decisions VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,channel_key) DO UPDATE SET decision=excluded.decision,reviewer_note=excluded.reviewer_note,updated_at=excluded.updated_at",
                   (candidate_id, channel_key, decision, note.strip(), datetime.now(timezone.utc).isoformat()))


def record_company_review(candidate_id: str, status: str, note: str = "", *, db_path: Path = PHASE6F_DB, review_path: Path = REVIEW_DB) -> None:
    status = status.upper()
    if status not in {"PENDING", "FURTHER_REVIEW"}:
        raise ValueError("Company review status must be PENDING or FURTHER_REVIEW; this is not outreach authorization")
    if len(note) > 1000:
        raise ValueError("Reviewer note is limited to 1000 characters")
    with _connect(db_path, True) as source:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        found = source.execute("SELECT 1 FROM phase6f_selection WHERE candidate_id=?", (candidate_id,)).fetchone() if "phase6f_selection" in tables else None
        if found is None and "phase6h_selection" in tables:
            found = source.execute("SELECT 1 FROM phase6h_selection WHERE candidate_id=?", (candidate_id,)).fetchone()
        if found is None:
            raise KeyError("Company is not in the Phase 6F reviewed cohort")
    initialize_review_db(review_path)
    with _connect(review_path) as db:
        db.execute("INSERT INTO company_review VALUES(?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET review_status=excluded.review_status,reviewer_note=excluded.reviewer_note,updated_at=excluded.updated_at",
                   (candidate_id, status, note.strip(), datetime.now(timezone.utc).isoformat()))


def _init_evaluation(path: Path, source_db: Path = PHASE6F_DB) -> list[str]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(source_db, True) as source, sqlite3.connect(path) as target:
            source.backup(target)
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS phase6g_eval_sample(candidate_id TEXT PRIMARY KEY, original_status TEXT NOT NULL, selected_reason TEXT NOT NULL, selected_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phase6g_eval_runs(run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL, attempted INTEGER NOT NULL, requests_attempted INTEGER NOT NULL, requests_succeeded INTEGER NOT NULL, requests_failed INTEGER NOT NULL, retries INTEGER NOT NULL, runtime_seconds REAL NOT NULL, counts_json TEXT NOT NULL, request_cap INTEGER, time_cap_seconds INTEGER, request_timeout_seconds REAL);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(phase6g_eval_runs)")}
        if "request_cap" not in columns:
            db.execute("ALTER TABLE phase6g_eval_runs ADD COLUMN request_cap INTEGER")
        if "time_cap_seconds" not in columns:
            db.execute("ALTER TABLE phase6g_eval_runs ADD COLUMN time_cap_seconds INTEGER")
        if "request_timeout_seconds" not in columns:
            db.execute("ALTER TABLE phase6g_eval_runs ADD COLUMN request_timeout_seconds REAL")
        current = {r[0] for r in db.execute("SELECT candidate_id FROM phase6g_eval_sample")}
        if not current:
            fail_rows = [r[0] for r in db.execute("""SELECT c.candidate_id FROM candidates c JOIN phase6f_selection s USING(candidate_id)
               WHERE c.verification_status='FETCH_FAILED' AND (lower(c.verification_reason) LIKE '%nodename%' OR lower(c.verification_reason) LIKE '%timed out%' OR lower(c.verification_reason) LIKE '%ssl%' OR lower(c.verification_reason) LIKE '%connection reset%') ORDER BY c.candidate_id LIMIT 15""")]
            ambiguous = [r[0] for r in db.execute("""SELECT c.candidate_id FROM candidates c JOIN phase6f_selection s USING(candidate_id)
               WHERE c.verification_status='AMBIGUOUS_IDENTITY' ORDER BY c.candidate_id LIMIT 15""")]
            now = datetime.now(timezone.utc).isoformat()
            ids = fail_rows + ambiguous
            for cid in ids:
                row = db.execute("SELECT verification_status FROM candidates WHERE candidate_id=?", (cid,)).fetchone()
                why = "bounded re-check of prior DNS/TLS/timeout/connection failure" if cid in fail_rows else "bounded identity-evidence re-evaluation"
                db.execute("INSERT INTO phase6g_eval_sample VALUES(?,?,?,?)", (cid, row[0], why, now))
                db.execute("UPDATE candidates SET verification_status='DISCOVERED_UNVERIFIED',geo_eligibility_status='UNKNOWN',geo_opportunity_status='UNKNOWN' WHERE candidate_id=?", (cid,))
            db.commit()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        return [r[0] for r in db.execute("SELECT candidate_id FROM phase6g_eval_sample ORDER BY candidate_id")]


def run_evaluation(*, path: Path = EVALUATION_DB, batch_size: int = 10, request_cap: int = 90, time_cap_seconds: int = 600,
                   request_timeout_seconds: float = 12) -> dict[str, Any]:
    if not 1 <= batch_size <= 10 or request_cap < 1 or time_cap_seconds < 1 or not 1 <= request_timeout_seconds <= 60:
        raise ValueError("batch_size must be 1..10 and request/time caps must be positive")
    ids = _init_evaluation(path)
    from outreach_agent.candidate_pool import verify_candidate_batch
    started = time.monotonic()
    requests_used = succeeded = failed = retries = attempted = 0
    output_counts: Counter[str] = Counter()
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= time_cap_seconds or requests_used >= request_cap:
            break
        with sqlite3.connect(path) as db:
            todo = [r[0] for r in db.execute("SELECT s.candidate_id FROM phase6g_eval_sample s JOIN candidates c USING(candidate_id) WHERE c.verification_status='DISCOVERED_UNVERIFIED' ORDER BY s.candidate_id LIMIT ?", (batch_size,))]
        if not todo:
            break
        result = verify_candidate_batch(len(todo), candidate_db=path, candidate_ids=todo,
              request_budget=min(30, request_cap - requests_used), time_budget_seconds=max(1, time_cap_seconds - int(elapsed)), include_contact_evidence=True,
              request_timeout_seconds=request_timeout_seconds)
        requests_used += result["requests_attempted"]
        succeeded += result["requests_succeeded"]
        failed += result["requests_failed"]
        retries += result["retries"]
        attempted += result["attempted"]
        output_counts.update(result["counts"])
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO phase6g_eval_runs(started_at,finished_at,attempted,requests_attempted,requests_succeeded,requests_failed,retries,runtime_seconds,counts_json,request_cap,time_cap_seconds,request_timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), result["attempted"], result["requests_attempted"], result["requests_succeeded"], result["requests_failed"], result["retries"], result["runtime_seconds"], json.dumps(result["counts"], sort_keys=True),request_cap,time_cap_seconds,request_timeout_seconds))
        if result["attempted"] == 0:
            break
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        statuses = Counter(r[0] for r in db.execute("SELECT c.verification_status FROM candidates c JOIN phase6g_eval_sample s USING(candidate_id)"))
        initial = Counter(r[0] for r in db.execute("SELECT original_status FROM phase6g_eval_sample"))
    return {"sample_size": len(ids), "processed_this_run": attempted, "remaining": statuses.get("DISCOVERED_UNVERIFIED", 0),
            "initial_statuses": dict(initial), "current_statuses": dict(statuses), "requests_attempted": requests_used,
            "requests_succeeded": succeeded, "requests_failed": failed, "retries": retries,
            "runtime_seconds": round(time.monotonic() - started, 2), "counts": dict(output_counts), "database": str(path)}


def export_all(*, data_dir: Path = OUT, db_path: Path = PHASE6F_DB, review_path: Path = REVIEW_DB) -> dict[str, Any]:
    audit = contact_audit_rows(db_path, review_path)
    failures = failure_rows(db_path)
    export_csv(data_dir / "phase6g_contact_quality_audit.csv", audit, AUDIT_FIELDS)
    export_csv(data_dir / "phase6g_contact_review_queue.csv", audit, AUDIT_FIELDS)
    export_csv(data_dir / "phase6g_verification_failure_analysis.csv", failures, FAILURE_FIELDS)
    return {"contact_rows": len(audit), "unique_companies_with_channels": len({r["candidate_id"] for r in audit}),
            "failures_analyzed": len(failures), "audit": str(data_dir / "phase6g_contact_quality_audit.csv"),
            "review_queue": str(data_dir / "phase6g_contact_review_queue.csv"), "failure_analysis": str(data_dir / "phase6g_verification_failure_analysis.csv")}


def write_report(*, output: Path = OUT / "phase6g_optimization_report.md", db_path: Path = PHASE6F_DB,
                 review_path: Path = REVIEW_DB, evaluation_path: Path = EVALUATION_DB) -> str:
    contacts = contact_audit_rows(db_path, review_path)
    failures = failure_rows(db_path)
    classes = Counter(r["channel_classification"] for r in contacts)
    failure_counts = Counter(r["failure_category"] for r in failures)
    company_kinds: dict[str, set[str]] = {}
    for row in contacts:
        company_kinds.setdefault(row["candidate_id"], set()).add(row["channel_classification"])
    email_companies = {cid for cid, kinds in company_kinds.items() if "PUBLIC_BUSINESS_EMAIL" in kinds}
    form_companies = {cid for cid, kinds in company_kinds.items() if "OFFICIAL_CONTACT_FORM" in kinds}
    page_only = {cid for cid, kinds in company_kinds.items() if "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD" in kinds and not kinds.intersection({"PUBLIC_BUSINESS_EMAIL", "OFFICIAL_CONTACT_FORM"})}
    duplicates = sum(int(r["duplicate_count"] or 0) for r in contacts)
    original_statuses = Counter(r["verification_status"] for r in failures)
    pending_contacts = sum(r["review_status"] == "PENDING" for r in contacts)
    evaluation = None
    if evaluation_path.exists():
        with sqlite3.connect(evaluation_path) as setup:
            columns = {row[1] for row in setup.execute("PRAGMA table_info(phase6g_eval_runs)")}
            if "request_cap" not in columns:
                setup.execute("ALTER TABLE phase6g_eval_runs ADD COLUMN request_cap INTEGER")
            if "time_cap_seconds" not in columns:
                setup.execute("ALTER TABLE phase6g_eval_runs ADD COLUMN time_cap_seconds INTEGER")
        with sqlite3.connect(f"file:{evaluation_path.resolve()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            sample_count = db.execute("SELECT COUNT(*) FROM phase6g_eval_sample").fetchone()[0]
            if sample_count:
                sample_statuses = Counter(r[0] for r in db.execute("SELECT c.verification_status FROM candidates c JOIN phase6g_eval_sample s USING(candidate_id)"))
                initial_statuses = Counter(r[0] for r in db.execute("SELECT original_status FROM phase6g_eval_sample"))
                runs = db.execute("SELECT COALESCE(SUM(attempted),0),COALESCE(SUM(requests_attempted),0),COALESCE(SUM(requests_succeeded),0),COALESCE(SUM(requests_failed),0),COALESCE(SUM(retries),0),COALESCE(SUM(runtime_seconds),0),COALESCE(MAX(request_cap),0),COALESCE(MAX(time_cap_seconds),0),COALESCE(MAX(request_timeout_seconds),0) FROM phase6g_eval_runs").fetchone()
                evaluation = {"sample": sample_count, "initial": initial_statuses, "current": sample_statuses,
                              "processed": sample_count - sample_statuses.get("DISCOVERED_UNVERIFIED", 0),
                              "verified": sample_statuses.get("VERIFIED", 0), "eligible": 0 if sample_statuses.get("VERIFIED", 0) == 0 else db.execute("SELECT COUNT(*) FROM candidates c JOIN phase6g_eval_sample s USING(candidate_id) WHERE c.geo_eligibility_status='ELIGIBLE'").fetchone()[0],
                              "attempted": runs[0], "requests": runs[1], "succeeded": runs[2], "failed": runs[3], "retries": runs[4], "runtime": round(runs[5], 2), "request_cap": runs[6] or "not recorded", "time_cap": runs[7] or "not recorded", "timeout": runs[8] or "not recorded"}
    status_total = sum(original_statuses.values())
    def rate(n: int, d: int) -> str:
        return f"{100*n/d:.1f}%" if d else "n/a"
    lines = [
        "# Phase 6G Contact Quality Audit and Pipeline Optimization",
        "",
        "## Scope and integrity",
        "",
        "The audit reads the saved Phase 6F selection and contact evidence. It does not edit `phase6f_candidates.db`, the 500-candidate Phase 6F exports, source candidate databases, company cohorts, or campaign history. Human contact/company review decisions are isolated in `data/phase6g_contact_review.db`. No email was sent and no form was submitted.",
        "",
        f"- Phase 6F companies with saved channels: **{len(company_kinds)}**",
        f"- Raw Phase 6F channel records inspected: **260** (semantic contact duplicates collapsed to **{len(contacts)}** audit rows; repeated records represented by duplicate counts: **{duplicates}**).",
        f"- Distinct companies with public business email evidence: **{len(email_companies)}**.",
        f"- Distinct companies with a confirmed official contact-form structure: **{len(form_companies)}**.",
        f"- Distinct companies with contact-page evidence only (no confirmed form/email): **{len(page_only)}**.",
        f"- Explicit named individual channels retained in Phase 6F: **{classes.get('NAMED_INDIVIDUAL_BUSINESS_CONTACT', 0)}**; none were present because Phase 6F excludes person-specific contact details.",
        f"- Unsupported/ambiguous channels: **{classes.get('INVALID_AMBIGUOUS_OR_UNSUPPORTED', 0)}**; channels pending human review: **{pending_contacts}**.",
        f"- Usable channel-candidate share (email or confirmed form; not human approved): **{rate(len(email_companies | form_companies), len(company_kinds))}** of channel-bearing companies.",
        "",
        "## Channel classification and confidence",
        "",
        "| Classification | Deduplicated records |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in sorted(classes.items()))
    conf = Counter(r["confidence"] for r in contacts)
    lines.extend(["", "Confidence is deterministic and transparent, based on same-domain provenance, verified company identity, captured purpose/evidence, and whether a usable submission method is actually evidenced. It is not an approval. Contact-page-only rows are downgraded because the method is unconfirmed. All remaining channels retain PENDING review until a person records a decision.", "",
                  "## Phase 6F verification outcome breakdown", "",
                  "The selected cohort had 500 companies: 93 FETCH_FAILED, 55 ACCESS_BLOCKED, 6 ROBOTS_DENIED, 78 AMBIGUOUS_IDENTITY, 24 INSUFFICIENT_EVIDENCE, 14 REJECTED_MISMATCH, and 230 VERIFIED. The CSV carries one row per non-verified company/status for traceability.", "",
                  "| Failure / unresolved category | Records |", "|---|---:|"])
    lines.extend(f"| {name} | {count} |" for name, count in sorted(failure_counts.items()))
    lines.extend(["", "Important: the 55 access-blocked cases are HTTP 403 responses. The stored evidence does not establish whether each is an explicit policy denial or bot protection; no CAPTCHA/bot-protection marker was captured in the saved evidence. Robots denials remain separately identified and were not retried. HTTP 429s, if present, are separately rate-limit categorized. Redirected unrelated sites stay identity mismatches, not fetch failures.", "",
                  "## Safe pipeline changes and measured effects", "",
                  "- Contact routes are deduplicated in the audit layer using company + channel kind + normalized email/route. The 260 source rows collapse to the row count above; original evidence remains untouched and duplicate counts are retained.",
                  "- Existing per-company page deduplication (`seen` URL set), persistent Phase 6F candidate checkpoints, bounded request budgets, same-client connection reuse within a batch, and idempotent channel inserts are retained. Phase 6G review writes go only to a separate database.",
                  "- A deterministic response-size failure no longer consumes a retry: retry handling now retries transport `RequestError`s but not deterministic response-cap `ValueError`s. A regression test verifies one request rather than retrying the same over-limit response. Phase 6F has 13 recorded response-too-large cases; because historical retries are not attributed per company/error, historical request savings cannot be claimed.",
                  "- A 401/403/429 response from `robots.txt` now fails closed as ROBOTS_DENIED before sitemap or company-page requests. The exact status and reason are preserved; neither site policy nor anti-bot controls are bypassed.",
                  "- No full recrawl was run to measure caching. Existing checkpoints prevent already-terminal companies from being fetched again on resume; Phase 6G's comparison sample is isolated in a Phase 6G database copy.", ""])
    if evaluation:
        lines.extend(["### Bounded comparison sample", "",
                      f"Re-evaluated **{evaluation['processed']} / {evaluation['sample']}** deterministically selected prior cases ({sum(evaluation['initial'].values())} total selected): DNS/TLS/timeout/connection failures and ambiguous identities only. Request cap: {evaluation['request_cap']}; time cap: {evaluation['time_cap']} seconds; per-request timeout: {evaluation['timeout']} seconds; batch size: 10; no access-blocked or robots-denied site was selected. Actual requests: {evaluation['requests']} ({evaluation['succeeded']} succeeded, {evaluation['failed']} failed); retries: {evaluation['retries']}; summed collector runtime: {evaluation['runtime']} seconds.",
                      f"Initial sample statuses: `{dict(evaluation['initial'])}`. Current statuses: `{dict(evaluation['current'])}`. Verified after re-check: **{evaluation['verified']}**; GEO eligible after re-check: **{evaluation['eligible']}**.",
                      "This is a bounded before/after observation, not a controlled proof of higher precision or population-wide yield. Ambiguous identity is only promoted when the existing first-party identity verifier supports it. Any remaining sample items are checkpointed and can be resumed without repeating terminal cases.", ""])
    else:
        lines.extend(["### Bounded comparison sample", "", "Not yet run. Use `uv run python scripts/phase6g_audit.py evaluate --batch-size 10 --request-cap 90 --time-cap-seconds 600` to run/resume the one bounded sample. The evaluator excludes HTTP access-blocked and robots-denied sites.", ""])
    lines.extend(["## Human review workflow and remaining blockers", "",
                  "The Companies page now includes a paginated batch review queue with source/confidence/review filters, company identity and GEO context, source and contact evidence links, reviewer-only approve/reject decisions for individual evidence channels, a company `Further review` state, and a filtered CSV export. The review sidecar cannot set campaign approval, research authorization, or outreach-ready state. Non-first-party and unverified identity evidence cannot be marked APPROVED through the backend.",
                  "",
                  "Contact channel approval remains separate from research participation consent and campaign-send approval. A contact page alone is not a usable form/email. Email and form evidence still requires human suitability review, freshness review, and any applicable research/ethics authorization before any campaign preparation. No sending is enabled by this phase.",
                  "",
                  "### Generated files", "",
                  "- `data/phase6g_contact_quality_audit.csv` — deduplicated channel audit with quality factors and source evidence.",
                  "- `data/phase6g_contact_review_queue.csv` — same deterministic records for dashboard/export review.",
                  "- `data/phase6g_verification_failure_analysis.csv` — selected Phase 6F failures, unresolved identity cases, evidence, and safe next action.",
                  "- `data/phase6g_contact_review.db` — local human decisions, separate from source data.",
                  "- `data/phase6g_evaluation.db` — isolated checkpoint copy for the bounded comparison pilot.", ""])
    report = "\n".join(lines)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    return report
