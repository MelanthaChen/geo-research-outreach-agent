"""Resumable continuation of the Phase 6F candidate verification pipeline."""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from outreach_agent.candidate_pool import ROOT, verify_candidate_batch
from outreach_agent.normalization import normalize_domain
from outreach_agent.phase6e_screening import PHASE6E_DB
from outreach_agent.phase6f import PHASE6F_DB, TOP5K

PHASE6H_DB = ROOT / "data/phase6h_candidates.db"
OUT = ROOT / "data"
RUN_TABLES = """
CREATE TABLE IF NOT EXISTS phase6h_selection (
 candidate_id TEXT PRIMARY KEY, selection_rank INTEGER NOT NULL, selection_reason TEXT NOT NULL,
 selected_from TEXT NOT NULL, selected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase6h_shortlist (
 candidate_id TEXT PRIMARY KEY, shortlist_rank INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS phase6h_runs (
 run_id INTEGER PRIMARY KEY, batch_number INTEGER NOT NULL, chunk_number INTEGER NOT NULL,
 batch_size INTEGER NOT NULL DEFAULT 250,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, attempted INTEGER NOT NULL,
 requests_attempted INTEGER NOT NULL, requests_succeeded INTEGER NOT NULL, requests_failed INTEGER NOT NULL,
 retries INTEGER NOT NULL, runtime_seconds REAL NOT NULL, request_budget INTEGER NOT NULL,
 time_budget_seconds REAL NOT NULL, counts_json TEXT NOT NULL, stop_reason TEXT
);
"""


def initialize(path: Path = PHASE6H_DB, source: Path = PHASE6F_DB) -> None:
    """Create the continuation by SQLite backup once; never overwrite an existing checkpoint."""
    if not path.exists():
        if not source.exists():
            raise FileNotFoundError(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src, sqlite3.connect(path) as dst:
            src.backup(dst)
    with sqlite3.connect(path) as db:
        db.executescript(RUN_TABLES)
        columns = {row[1] for row in db.execute("PRAGMA table_info(phase6h_runs)")}
        if "batch_size" not in columns:
            db.execute("ALTER TABLE phase6h_runs ADD COLUMN batch_size INTEGER NOT NULL DEFAULT 250")
        db.commit()


def reconcile(shortlist: Path = TOP5K, phase6e_db: Path = PHASE6E_DB,
              phase6f_db: Path = PHASE6F_DB) -> dict[str, Any]:
    """Read-only exact status reconciliation of the frozen shortlist."""
    with shortlist.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ids = [row.get("candidate_id", "") for row in rows]
    unique = set(ids)
    with sqlite3.connect(f"file:{phase6e_db.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        status = {row["candidate_id"]: row["verification_status"] for row in db.execute(
            f"SELECT candidate_id,verification_status FROM candidates WHERE candidate_id IN ({','.join('?' for _ in unique)})", tuple(unique))}
    with sqlite3.connect(f"file:{phase6f_db.resolve()}?mode=ro", uri=True) as db:
        selected = {row[0] for row in db.execute("SELECT candidate_id FROM phase6f_selection")}
        statuses6f = Counter(row[0] for row in db.execute("SELECT c.verification_status FROM candidates c JOIN phase6f_selection s USING(candidate_id)"))
    prior_terminal = sum(value != "DISCOVERED_UNVERIFIED" for value in status.values())
    unresolved = {cid for cid, value in status.items() if value == "DISCOVERED_UNVERIFIED"}
    already_attempted = unresolved & selected
    missing = unique - set(status)
    return {
        "shortlist_rows": len(rows), "shortlist_unique_ids": len(unique), "missing_from_phase6e": len(missing),
        "phase6e_statuses": dict(sorted(Counter(status.values()).items())),
        "phase6e_terminal_before_phase6f": prior_terminal,
        "phase6f_selected_in_shortlist": len(selected & unique), "phase6f_statuses": dict(sorted(statuses6f.items())),
        "already_attempted_but_unresolved_in_phase6e": len(already_attempted),
        "remaining_first_pass_workload": len(unresolved - already_attempted),
    }


def select_remaining(*, shortlist: Path = TOP5K, phase6e_db: Path = PHASE6E_DB,
                     phase6f_db: Path = PHASE6F_DB, db_path: Path = PHASE6H_DB) -> int:
    """Persist, in stable shortlist order, only never-attempted OSM candidates."""
    initialize(db_path, source=phase6f_db)
    with shortlist.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with sqlite3.connect(f"file:{phase6e_db.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        source_rows = {r["candidate_id"]: dict(r) for r in db.execute("SELECT * FROM candidates")}
    with sqlite3.connect(f"file:{phase6f_db.resolve()}?mode=ro", uri=True) as db:
        attempted = {r[0] for r in db.execute("SELECT candidate_id FROM phase6f_selection")}
    selected: list[tuple[str, str]] = []
    for rank, row in enumerate(rows, 1):
        cid = row.get("candidate_id", "")
        candidate = source_rows.get(cid)
        if not candidate or not cid.startswith("osm:") or cid in attempted:
            continue
        if candidate.get("verification_status") != "DISCOVERED_UNVERIFIED":
            continue
        # Preserve Phase 6F's quality-first tie-breaks, but retain the Top-5,000 rank last.
        likely = row.get("screen_status") == "LIKELY_FIT"
        independent = row.get("chain_location_concern") == "NO_STRUCTURED_SIGNAL"
        clear_domain = row.get("website_domain_concern") == "NO_OBVIOUS_PATTERN"
        priority = int(row.get("priority_score") or 0)
        selected.append((cid, json.dumps((not likely, not independent, not clear_domain, -priority, candidate["canonical_domain"], rank))))
    with sqlite3.connect(db_path) as db:
        current = db.execute("SELECT COUNT(*) FROM phase6h_selection").fetchone()[0]
        if current and current != len(selected):
            raise ValueError(f"Existing Phase 6H selection has {current} rows, reconciled workload now has {len(selected)}; refusing to replace checkpoint")
        if not current:
            stamp = datetime.now(timezone.utc).isoformat()
            for selection_rank, (cid, reason) in enumerate(selected, 1):
                db.execute("INSERT INTO phase6h_selection VALUES(?,?,?,?,?)", (cid, selection_rank, reason, str(shortlist), stamp))
        shortlist_count = db.execute("SELECT COUNT(*) FROM phase6h_shortlist").fetchone()[0]
        if shortlist_count and shortlist_count != len(rows):
            raise ValueError("Existing Phase 6H shortlist membership differs from the frozen input; refusing to replace it")
        if not shortlist_count:
            for rank, row in enumerate(rows, 1):
                db.execute("INSERT INTO phase6h_shortlist VALUES(?,?)", (row["candidate_id"], rank))
        db.commit()
    return len(selected)


def _extract_channels(db_path: Path) -> int:
    """Reuse saved first-party parser evidence in the inherited candidate contact table."""
    inserted = 0
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        candidates = db.execute("""SELECT c.candidate_id,c.canonical_domain,c.verification_status,
          c.geo_eligibility_status,c.evidence_json FROM candidates c JOIN phase6h_selection s USING(candidate_id)
          WHERE c.verification_status='VERIFIED' AND c.geo_eligibility_status='ELIGIBLE'""").fetchall()
        for candidate in candidates:
            try:
                evidence = json.loads(candidate["evidence_json"] or "{}")
            except json.JSONDecodeError:
                continue
            for page in evidence.get("pages", []):
                page_url = (page.get("final_url") or page.get("requested_url") or "").strip()
                if page_url:
                    page_path = urlsplit(page_url).path.casefold()
                    if any(part in page_path for part in ("contact", "partnership", "business-inquir", "get-in-touch")):
                        try:
                            if normalize_domain(page_url) == candidate["canonical_domain"]:
                                inserted += db.execute("""INSERT OR IGNORE INTO phase6f_contact_channels
                                  (candidate_id,channel_type,email,channel_url,source_url,evidence,purpose,review_status)
                                  VALUES(?,?,?,?,?,?,?,'PENDING')""",
                                  (candidate["candidate_id"], "CONTACT_PAGE", None, page_url, page_url,
                                   "Official same-domain contact/business page fetched by bounded crawler", "Contact route; no submission method inferred")).rowcount
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
                    if kind not in {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "CONTACT_FORM"} or not url or not source_url:
                        continue
                    try:
                        domain = candidate["canonical_domain"]
                        if normalize_domain(source_url) != domain or normalize_domain(url) != domain:
                            continue
                        if email and ("@" not in email or normalize_domain("https://" + email.rsplit("@", 1)[-1]) != domain):
                            continue
                        inserted += db.execute("""INSERT OR IGNORE INTO phase6f_contact_channels
                          (candidate_id,channel_type,email,channel_url,source_url,evidence,purpose,review_status)
                          VALUES(?,?,?,?,?,?,?,'PENDING')""",
                          (candidate["candidate_id"], kind, email, url, source_url,
                           str(channel.get("evidence") or "")[:1200], str(channel.get("purpose") or "")[:500])).rowcount
                    except ValueError:
                        continue
        db.execute("UPDATE candidates SET contact_status='CONTACT_FOUND' WHERE candidate_id IN (SELECT DISTINCT candidate_id FROM phase6f_contact_channels WHERE candidate_id IN (SELECT candidate_id FROM phase6h_selection))")
        db.commit()
    return inserted


def _selection_totals(db_path: Path = PHASE6H_DB) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        selected = db.execute("SELECT COUNT(*) FROM phase6h_selection").fetchone()[0]
        records = db.execute("SELECT c.verification_status,c.geo_eligibility_status,c.contact_status FROM candidates c JOIN phase6h_selection s USING(candidate_id)").fetchall()
        statuses = Counter(row["verification_status"] for row in records)
        runs = db.execute("SELECT COALESCE(SUM(requests_attempted),0),COALESCE(SUM(requests_succeeded),0),COALESCE(SUM(requests_failed),0),COALESCE(SUM(retries),0),COALESCE(SUM(runtime_seconds),0) FROM phase6h_runs").fetchone()
        channels = db.execute("SELECT COUNT(*) FROM phase6f_contact_channels ch JOIN phase6h_selection s USING(candidate_id)").fetchone()[0]
        email_company_count = db.execute("SELECT COUNT(DISTINCT candidate_id) FROM phase6f_contact_channels ch JOIN phase6h_selection s USING(candidate_id) WHERE email IS NOT NULL").fetchone()[0]
        forms = db.execute("SELECT COUNT(DISTINCT candidate_id) FROM phase6f_contact_channels ch JOIN phase6h_selection s USING(candidate_id) WHERE channel_type='CONTACT_FORM'").fetchone()[0]
        page_only = db.execute("""SELECT COUNT(DISTINCT ch.candidate_id) FROM phase6f_contact_channels ch JOIN phase6h_selection s USING(candidate_id)
          WHERE ch.channel_type='CONTACT_PAGE' AND NOT EXISTS(SELECT 1 FROM phase6f_contact_channels x WHERE x.candidate_id=ch.candidate_id AND (x.email IS NOT NULL OR x.channel_type='CONTACT_FORM'))""").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM phase6f_contact_channels ch JOIN phase6h_selection s USING(candidate_id) WHERE review_status='PENDING'").fetchone()[0]
    processed = selected - statuses.get("DISCOVERED_UNVERIFIED", 0)
    return {"selected": selected, "processed_new": processed, "pending_verification": statuses.get("DISCOVERED_UNVERIFIED", 0),
            "verified_new": statuses.get("VERIFIED", 0), "geo_eligible_new": sum(r["geo_eligibility_status"] == "ELIGIBLE" for r in records),
            "contact_channels_new": channels, "companies_with_business_emails_new": email_company_count,
            "companies_with_confirmed_forms_new": forms, "contact_page_only_companies_new": page_only,
            "pending_channel_reviews_new": pending,
            "verification_statuses_new": dict(sorted(statuses.items())),
            "requests_attempted": runs[0], "requests_succeeded": runs[1], "requests_failed": runs[2], "retries": runs[3],
            "runtime_seconds": round(runs[4], 2)}


def run(*, db_path: Path = PHASE6H_DB, shortlist: Path = TOP5K, batch_size: int = 250,
        request_cap: int = 10000, time_cap_seconds: int = 36000,
        chunk_request_budget: int = 500, request_timeout_seconds: float = 12,
        retry_count: int = 1, delay_between_requests_seconds: float = 1,
        output_dir: Path = OUT, review_path: Path | None = None,
        phase6e_db: Path = PHASE6E_DB, phase6f_db: Path = PHASE6F_DB) -> dict[str, Any]:
    if not 1 <= batch_size <= 500 or request_cap < 1 or time_cap_seconds < 1 or not 1 <= chunk_request_budget <= 500:
        raise ValueError("batch size must be 1..500; caps positive; chunk request budget 1..500")
    initialize(db_path, source=phase6f_db)
    selection_count = select_remaining(shortlist=shortlist, phase6e_db=phase6e_db, phase6f_db=phase6f_db, db_path=db_path)
    processed_before = _selection_totals(db_path)["processed_new"]
    started = time.monotonic()
    current_batch: int | None = None
    chunk_number = 0
    stop_reason = None
    while True:
        totals = _selection_totals(db_path)
        if totals["requests_attempted"] >= request_cap:
            stop_reason = "request_cap_exhausted"
            break
        if totals["runtime_seconds"] + time.monotonic() - started >= time_cap_seconds:
            stop_reason = "time_cap_exhausted"
            break
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            todo = [r["candidate_id"] for r in db.execute("""SELECT c.candidate_id FROM candidates c
              JOIN phase6h_selection s USING(candidate_id) WHERE c.verification_status='DISCOVERED_UNVERIFIED'
              ORDER BY s.selection_rank LIMIT ?""", (min(batch_size, 100),))]
            if not todo:
                break
            ranks = [db.execute("SELECT selection_rank FROM phase6h_selection WHERE candidate_id=?", (cid,)).fetchone()[0] for cid in todo]
            batch_number = (min(ranks) - 1) // batch_size + 1
            if current_batch != batch_number:
                current_batch, chunk_number = batch_number, 0
            chunk_number += 1
        elapsed_total = totals["runtime_seconds"] + time.monotonic() - started
        remaining_time = max(1, time_cap_seconds - int(elapsed_total))
        remaining_requests = request_cap - totals["requests_attempted"]
        budget = min(chunk_request_budget, remaining_requests)
        stamp = datetime.now(timezone.utc).isoformat()
        result = verify_candidate_batch(len(todo), candidate_db=db_path, candidate_ids=todo,
            request_budget=budget, time_budget_seconds=remaining_time, include_contact_evidence=True,
            request_timeout_seconds=request_timeout_seconds, retry_count=retry_count,
            delay_between_requests_seconds=delay_between_requests_seconds)
        _extract_channels(db_path)
        finished = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as db:
            db.execute("""INSERT INTO phase6h_runs(batch_number,chunk_number,batch_size,started_at,finished_at,attempted,requests_attempted,
              requests_succeeded,requests_failed,retries,runtime_seconds,request_budget,time_budget_seconds,counts_json,stop_reason)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (batch_number, chunk_number, batch_size, stamp, finished, result["attempted"], result["requests_attempted"],
               result["requests_succeeded"], result["requests_failed"], result["retries"], result["runtime_seconds"],
               budget, remaining_time, json.dumps(result.get("counts", {}), sort_keys=True), None))
            db.commit()
        if result.get("request_budget_exhausted") and result["attempted"] == 0:
            stop_reason = "request_cap_exhausted"
            break
        if result.get("time_budget_exhausted"):
            stop_reason = "time_cap_exhausted"
            break
        if result["attempted"] == 0:
            stop_reason = "no_progress"
            break
        # Assess each completed 250-company (or configured-size) batch against a preregistered
        # half-Phase-6F yield floor. Stop safely instead of scaling a materially degraded run.
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
            batch_rows = db.execute("""SELECT c.verification_status,c.geo_eligibility_status FROM candidates c
              JOIN phase6h_selection s USING(candidate_id) WHERE s.selection_rank BETWEEN ? AND ?
              AND c.verification_status<>'DISCOVERED_UNVERIFIED'""", ((batch_number-1)*batch_size+1, batch_number*batch_size)).fetchall()
        batch_start, batch_end = (batch_number-1)*batch_size+1, batch_number*batch_size
        expected_size = min(batch_size, max(0, selection_count-batch_start+1))
        if len(batch_rows) >= expected_size and expected_size:
            verified_rate = sum(r[0] == "VERIFIED" for r in batch_rows) / expected_size
            eligible_rate = sum(r[1] == "ELIGIBLE" for r in batch_rows) / expected_size
            if verified_rate < 0.23 or eligible_rate < 0.204:
                stop_reason = "quality_degradation_pause"
                break
        if result.get("request_budget_exhausted"):
            stop_reason = "request_cap_exhausted"
            break
    _extract_channels(db_path)
    if stop_reason is None:
        stop_reason = "selection_complete"
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE phase6h_runs SET stop_reason=? WHERE run_id=(SELECT MAX(run_id) FROM phase6h_runs)", (stop_reason,))
        db.commit()
    export_outputs(db_path=db_path, output_dir=output_dir, review_path=review_path)
    totals = _selection_totals(db_path)
    totals.update({"new_first_pass_workload": selection_count, "processed_this_invocation": totals["processed_new"] - processed_before,
                   "runtime_seconds_this_invocation": round(time.monotonic()-started, 2), "stop_reason": stop_reason or "selection_complete"})
    return totals


def execution_metrics(db_path: Path = PHASE6H_DB, *, review_path: Path | None = None) -> list[dict[str, Any]]:
    """Export exact request/time totals with the company outcomes in each checkpoint chunk."""
    from outreach_agent.phase6g import contact_audit_rows
    review_path = review_path or (ROOT / "data/phase6g_contact_review.db")
    audited = contact_audit_rows(db_path, review_path)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        checkpoints = [dict(row) for row in db.execute("SELECT * FROM phase6h_runs ORDER BY run_id")]
        records = [dict(row) for row in db.execute("""SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status,
          s.selection_rank FROM candidates c JOIN phase6h_selection s USING(candidate_id)""")]
        selection_count = db.execute("SELECT COUNT(*) FROM phase6h_selection").fetchone()[0]
    result = []
    rank_cursor = 1
    for checkpoint in checkpoints:
        attempted = int(checkpoint["attempted"])
        size = int(checkpoint["batch_size"])
        start = rank_cursor
        end = min(selection_count, start + attempted - 1) if attempted else start - 1
        rank_cursor += attempted
        processed = [row for row in records if start <= row["selection_rank"] <= end]
        verified = [row for row in processed if row["verification_status"] == "VERIFIED"]
        eligible = [row for row in processed if row["geo_eligibility_status"] == "ELIGIBLE"]
        ids = {row["candidate_id"] for row in processed}
        batch_audit = [row for row in audited if row["candidate_id"] in ids]
        emails = {row["candidate_id"] for row in batch_audit if row["channel_classification"] == "PUBLIC_BUSINESS_EMAIL"}
        forms = {row["candidate_id"] for row in batch_audit if row["channel_classification"] == "OFFICIAL_CONTACT_FORM"}
        pages = {row["candidate_id"] for row in batch_audit if row["channel_classification"] == "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD"}
        usable = emails | forms
        statuses = Counter(row["verification_status"] for row in processed if row["verification_status"] != "VERIFIED")
        first_batch = (start - 1) // size + 1 if attempted else checkpoint["batch_number"]
        last_batch = (end - 1) // size + 1 if attempted else first_batch
        result.append({
            "run_id": checkpoint["run_id"], "cohort_batch_range": str(first_batch) if first_batch == last_batch else f"{first_batch}-{last_batch}",
            "checkpoint_chunk": checkpoint["chunk_number"],
            "selection_rank_start": start if attempted else "", "selection_rank_end": end if attempted else "",
            "processed": len(processed),
            "verified": len(verified), "identity_verification_rate": round(len(verified)/len(processed), 4) if processed else 0,
            "geo_eligible": len(eligible), "geo_eligibility_rate": round(len(eligible)/len(processed), 4) if processed else 0,
            "companies_with_usable_email_or_form": len(usable),
            "contact_discovery_rate_of_geo_eligible": round(len(usable)/len(eligible), 4) if eligible else 0,
            "companies_with_public_business_email": len(emails),
            "email_discovery_rate_of_geo_eligible": round(len(emails)/len(eligible), 4) if eligible else 0,
            "companies_with_confirmed_forms": len(forms),
            "form_discovery_rate_of_geo_eligible": round(len(forms)/len(eligible), 4) if eligible else 0,
            "contact_page_only_companies": len(pages - usable),
            "failure_distribution_json": json.dumps(dict(sorted(statuses.items())), sort_keys=True),
            "requests_attempted": checkpoint["requests_attempted"], "requests_succeeded": checkpoint["requests_succeeded"],
            "requests_failed": checkpoint["requests_failed"], "retries": checkpoint["retries"],
            "runtime_seconds": round(checkpoint["runtime_seconds"], 2), "stop_reason": checkpoint["stop_reason"] or "",
        })
    return result


def cohort_batch_metrics(db_path: Path = PHASE6H_DB) -> list[dict[str, Any]]:
    """Quality rates at the configured 250/500-candidate batch boundary."""
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        selection_count = db.execute("SELECT COUNT(*) FROM phase6h_selection").fetchone()[0]
        batch_size = db.execute("SELECT COALESCE(MAX(batch_size),250) FROM phase6h_runs").fetchone()[0]
        records = [dict(row) for row in db.execute("""SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status,s.selection_rank
          FROM candidates c JOIN phase6h_selection s USING(candidate_id) ORDER BY s.selection_rank""")]
    result = []
    for number, start in enumerate(range(1, selection_count + 1, batch_size), 1):
        end = min(selection_count, start + batch_size - 1)
        rows = [row for row in records if start <= row["selection_rank"] <= end]
        processed = [row for row in rows if row["verification_status"] != "DISCOVERED_UNVERIFIED"]
        verified = sum(row["verification_status"] == "VERIFIED" for row in processed)
        eligible = sum(row["geo_eligibility_status"] == "ELIGIBLE" for row in processed)
        if not processed:
            continue
        result.append({"batch_number": number, "rank_start": start, "rank_end": end, "selected": len(rows),
                       "processed": len(processed), "state": "COMPLETE" if rows and len(processed) == len(rows) else "PARTIAL",
                       "identity_verification_rate": round(verified/len(processed), 4) if processed else 0,
                       "geo_eligibility_rate": round(eligible/len(processed), 4) if processed else 0})
    return result


def export_outputs(*, db_path: Path = PHASE6H_DB, output_dir: Path = OUT,
                   review_path: Path | None = None) -> dict[str, str | int]:
    """Write deterministic evidence exports; all newly discovered channels remain pending."""
    from outreach_agent.phase6g import AUDIT_FIELDS, contact_audit_rows, csv_text
    initialize(db_path)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        selected = [dict(r) for r in db.execute("""SELECT c.*,s.shortlist_rank AS selection_rank FROM candidates c JOIN phase6h_shortlist s USING(candidate_id)
          ORDER BY s.shortlist_rank""")]
        failures = [r for r in selected if r["verification_status"] not in {"VERIFIED", "DISCOVERED_UNVERIFIED"}]
        channel_rows = [dict(r) for r in db.execute("""SELECT c.candidate_id,c.company_name,c.canonical_domain,c.source_name,c.source_url,
          c.verification_status,c.geo_eligibility_status,c.geo_opportunity_status,c.contact_status,c.last_inspected_at,
          ch.channel_type,ch.email,ch.channel_url,ch.source_url AS evidence_url,ch.evidence,ch.purpose,ch.review_status
          FROM phase6f_contact_channels ch JOIN candidates c USING(candidate_id) JOIN phase6h_shortlist s USING(candidate_id)
          ORDER BY c.canonical_domain,ch.channel_type,ch.email,ch.channel_url""")]
    verified = [r for r in selected if r["verification_status"] == "VERIFIED"]
    eligible = [r for r in verified if r["geo_eligibility_status"] == "ELIGIBLE"]
    review = contact_audit_rows(db_path, review_path or (Path(__file__).resolve().parents[2] / "data/phase6g_contact_review.db"))
    shortlist_ids = {c["candidate_id"] for c in selected}
    review = [r for r in review if r["candidate_id"] in shortlist_ids]
    emails = [r for r in review if r["channel_classification"] == "PUBLIC_BUSINESS_EMAIL"]
    forms = [r for r in review if r["channel_classification"] == "OFFICIAL_CONTACT_FORM"]
    def write(name: str, rows: list[dict[str, Any]], fields: list[str] | tuple[str, ...] | None = None) -> str:
        path = output_dir / name
        if fields is None:
            fields = list(rows[0].keys()) if rows else ["candidate_id"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        return str(path)
    outputs: dict[str, str | int] = {}
    outputs["verified"] = write("phase6h_verified_companies.csv", verified)
    outputs["geo_eligible"] = write("phase6h_geo_eligible_companies.csv", eligible)
    outputs["business_emails"] = write("phase6h_business_email_contacts.csv", emails)
    outputs["official_forms"] = write("phase6h_official_contact_forms.csv", forms)
    outputs["review_queue"] = write("phase6h_contact_review_queue.csv", review, AUDIT_FIELDS)
    outputs["execution_metrics"] = write("phase6h_execution_metrics.csv", execution_metrics(db_path, review_path=review_path))
    return outputs


def report(*, db_path: Path = PHASE6H_DB, reconciliation: dict[str, Any] | None = None) -> str:
    totals = _selection_totals(db_path)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status FROM candidates c JOIN phase6h_shortlist s USING(candidate_id)")]
        new_rows = [dict(r) for r in db.execute("SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status FROM candidates c JOIN phase6h_selection s USING(candidate_id)")]
        channels = [dict(r) for r in db.execute("""SELECT ch.* FROM phase6f_contact_channels ch JOIN phase6h_shortlist s USING(candidate_id)""")]
    statuses = Counter(r["verification_status"] for r in rows)
    from outreach_agent.phase6g import contact_audit_rows
    review_rows = [r for r in contact_audit_rows(db_path, ROOT / "data/phase6g_contact_review.db")
                   if db_path.exists() and r["candidate_id"] in {row["candidate_id"] for row in rows}]
    new_ids = {row["candidate_id"] for row in new_rows}
    new_review_rows = [row for row in review_rows if row["candidate_id"] in new_ids]
    email_companies = {r["candidate_id"] for r in review_rows if r["channel_classification"] == "PUBLIC_BUSINESS_EMAIL"}
    form_companies = {r["candidate_id"] for r in review_rows if r["channel_classification"] == "OFFICIAL_CONTACT_FORM"}
    page_companies = {r["candidate_id"] for r in review_rows if r["channel_classification"] == "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD"}
    page_only = page_companies - email_companies - form_companies
    processed = len(rows)-statuses.get("DISCOVERED_UNVERIFIED", 0)
    pending_reviews = sum(r["review_status"] == "PENDING" for r in review_rows)
    new_pending_reviews = sum(r["review_status"] == "PENDING" for r in new_review_rows)
    new_statuses = Counter(row["verification_status"] for row in new_rows)
    new_email_companies = {r["candidate_id"] for r in new_review_rows if r["channel_classification"] == "PUBLIC_BUSINESS_EMAIL"}
    new_form_companies = {r["candidate_id"] for r in new_review_rows if r["channel_classification"] == "OFFICIAL_CONTACT_FORM"}
    new_page_companies = {r["candidate_id"] for r in new_review_rows if r["channel_classification"] == "OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD"}
    new_page_only = new_page_companies - new_email_companies - new_form_companies
    new_processed = len(new_rows) - new_statuses.get("DISCOVERED_UNVERIFIED", 0)
    batch_quality = cohort_batch_metrics(db_path)
    data = reconciliation or reconcile()
    lines = ["# Phase 6H: Scale Verification and Contact Discovery", "",
      "## Reconciled baseline", "", f"- Top 5,000 shortlist rows / unique IDs: {data['shortlist_rows']} / {data['shortlist_unique_ids']}",
      f"- Phase 6E outcomes already terminal: {data['phase6e_terminal_before_phase6f']}",
      f"- Phase 6F candidates already selected: {data['phase6f_selected_in_shortlist']}",
      f"- Exact remaining first-pass workload at start: {data['remaining_first_pass_workload']}",
      "- Candidate input is the Phase 6E OSM shortlist; no companies were acquired.", "",
      "## Phase 6H execution results", "", f"- Newly processed by Phase 6H: {new_processed:,} / {len(new_rows):,}",
      f"- Total processed in Top 5,000 shortlist: {processed:,} / {len(rows):,}",
      f"- Remaining first-pass work after this run: {new_statuses.get('DISCOVERED_UNVERIFIED', 0):,}",
      f"- Verified identities across shortlist: {statuses.get('VERIFIED', 0):,}",
      f"- GEO eligible under existing rules across shortlist: {sum(r['geo_eligibility_status']=='ELIGIBLE' for r in rows):,}",
      f"- Companies with public business email evidence: {len(email_companies):,}",
      f"- Companies with confirmed first-party contact form evidence: {len(form_companies):,}",
      f"- Contact-page-only companies: {len(page_only):,}",
      f"- Unique companies with any first-party channel evidence: {len(email_companies|form_companies|page_companies):,}",
      f"- Unique companies with usable email or confirmed form: {len(email_companies|form_companies):,}",
      f"- New unique companies with public business email: {len(new_email_companies):,}",
      f"- New unique companies with confirmed official contact forms: {len(new_form_companies):,}",
      f"- New contact-page-only companies: {len(new_page_only):,}",
      f"- New unique companies with usable channels (email/form): {len(new_email_companies | new_form_companies):,}",
      f"- New channel records pending human review: {new_pending_reviews:,} (total shortlist pending: {pending_reviews:,})",
      f"- HTTP attempts: {totals['requests_attempted']:,} ({totals['requests_succeeded']:,} successful responses; {totals['requests_failed']:,} failed; {totals['retries']:,} retries)",
      f"- Runtime: {totals['runtime_seconds']:.1f}s across persisted batches; invocation runtime is reported separately by CLI.",
      f"- Phase 6H status distribution: `{json.dumps(dict(sorted(new_statuses.items())), sort_keys=True)}`",
      f"- Full Top 5,000 status distribution: `{json.dumps(dict(sorted(statuses.items())), sort_keys=True)}`", "",
      "## Batch quality checks", "",
      "- Automatic pause floor: a completed batch pauses further requests if identity verification is below 23.0% or GEO eligibility below 20.4% (half of observed Phase 6F rates, 46.0% and 40.8%).",
      f"- 250-candidate cohort completion and yield rates: `{json.dumps(batch_quality, sort_keys=True)}`",
      "- Exact HTTP attempts, successes/failures, retries, and runtime are recorded per checkpoint chunk in `phase6h_execution_metrics.csv`.", "",
      "## Data handling and controls", "",
      "Phase 6H uses a one-time SQLite backup of the Phase 6F candidate database. The frozen Phase 6E/6F stores are read-only inputs and are not written. Every candidate checkpoint is committed by the existing verifier; each batch's requests, timing, counts, and stop reason are persisted in the Phase 6H run ledger. New contact evidence uses the inherited candidate contact table and stays PENDING in the existing Phase 6G review sidecar. Website fetching respects robots controls, rate limits, response/time/request caps, and does not bypass bot protection. No emails or forms were sent/submitted.", "",
      "## Output files", "", "- `data/phase6h_verified_companies.csv`", "- `data/phase6h_geo_eligible_companies.csv`", "- `data/phase6h_business_email_contacts.csv`", "- `data/phase6h_official_contact_forms.csv`", "- `data/phase6h_contact_review_queue.csv`", "- `data/phase6h_execution_metrics.csv`", "",
      "A full first-pass completion means every selected record has a terminal saved verification status. If a request/time budget, site access restriction, or the quality pause is reached, pending records remain explicit for resumption. GEO eligibility and channel evidence are not human approvals or outreach authorization.", ""]
    return "\n".join(lines)


def write_report(*, output: Path = OUT / "phase6h_final_report.md", db_path: Path = PHASE6H_DB,
                 reconciliation: dict[str, Any] | None = None) -> str:
    value = report(db_path=db_path, reconciliation=reconciliation)
    output.write_text(value, encoding="utf-8")
    return value


def integrity(path: Path = PHASE6H_DB) -> str:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def dashboard_summary(path: Path = PHASE6H_DB, shortlist: Path = TOP5K, *,
                     phase6e_db: Path = PHASE6E_DB, phase6f_db: Path = PHASE6F_DB) -> dict[str, Any]:
    baseline = reconcile(shortlist=shortlist, phase6e_db=phase6e_db, phase6f_db=phase6f_db)
    if not path.exists():
        return {"shortlist": baseline["shortlist_unique_ids"], "starting_workload": baseline["remaining_first_pass_workload"],
                "processed": 0, "remaining": baseline["remaining_first_pass_workload"], "verified": 0, "geo_eligible": 0,
                "companies_with_business_emails": 0, "companies_with_contact_forms": 0, "pending_contact_reviews": 0, "outreach_ready": 0}
    totals = _selection_totals(path)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        statuses = Counter(r[0] for r in db.execute("SELECT c.verification_status FROM candidates c JOIN phase6h_shortlist s USING(candidate_id)"))
        eligible = db.execute("SELECT COUNT(*) FROM candidates c JOIN phase6h_shortlist s USING(candidate_id) WHERE c.verification_status='VERIFIED' AND c.geo_eligibility_status='ELIGIBLE'").fetchone()[0]
        outreach_ready = db.execute("SELECT COUNT(*) FROM candidates c JOIN phase6h_shortlist s USING(candidate_id) WHERE c.outreach_ready_status='READY'").fetchone()[0]
    from outreach_agent.phase6g import contact_audit_rows
    reviewed = contact_audit_rows(path, ROOT / "data/phase6g_contact_review.db")
    shortlist_ids = set()
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        shortlist_ids = {row[0] for row in db.execute("SELECT candidate_id FROM phase6h_shortlist")}
    reviewed = [row for row in reviewed if row["candidate_id"] in shortlist_ids]
    email_companies = len({r["candidate_id"] for r in reviewed if r["channel_classification"] == "PUBLIC_BUSINESS_EMAIL"})
    form_companies = len({r["candidate_id"] for r in reviewed if r["channel_classification"] == "OFFICIAL_CONTACT_FORM"})
    pending_reviews = sum(r["review_status"] == "PENDING" for r in reviewed)
    return {"shortlist": baseline["shortlist_unique_ids"], "starting_workload": baseline["remaining_first_pass_workload"],
            "processed": baseline["shortlist_unique_ids"]-statuses.get("DISCOVERED_UNVERIFIED", 0),
            "remaining": statuses.get("DISCOVERED_UNVERIFIED", 0), "verified": statuses.get("VERIFIED", 0),
            "geo_eligible": eligible, "companies_with_business_emails": email_companies,
            "companies_with_contact_forms": form_companies,
            "pending_contact_reviews": pending_reviews, "outreach_ready": outreach_ready,
            "requests_attempted": totals["requests_attempted"], "runtime_seconds": totals["runtime_seconds"]}


def dashboard_overlays(candidate_ids: list[str], path: Path = PHASE6H_DB) -> dict[str, dict[str, Any]]:
    if not path.exists() or not candidate_ids:
        return {}
    marks = ",".join("?" for _ in candidate_ids)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"""SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status,c.geo_opportunity_status,
          c.contact_status,c.verification_reason,c.last_inspected_at FROM candidates c
          JOIN phase6h_shortlist s USING(candidate_id) WHERE c.candidate_id IN ({marks})""", candidate_ids).fetchall()
    return {row["candidate_id"]: dict(row) for row in rows}
