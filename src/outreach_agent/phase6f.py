"""Bounded, resumable Phase 6F verification of the Phase 6E OSM shortlist."""

from __future__ import annotations

import csv
import hashlib
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

PHASE6F_DB = ROOT / "data/phase6f_candidates.db"
TOP5K = ROOT / "data/phase6e_top_5000_candidates.csv"
OUT = ROOT / "data"
RUN_TABLES = """
CREATE TABLE IF NOT EXISTS phase6f_selection (
 candidate_id TEXT PRIMARY KEY, selection_rank INTEGER NOT NULL, selection_reason TEXT NOT NULL,
 selected_from TEXT NOT NULL, selected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase6f_runs (
 run_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
 attempted INTEGER NOT NULL, requests_attempted INTEGER NOT NULL, requests_succeeded INTEGER NOT NULL,
 requests_failed INTEGER NOT NULL, retries INTEGER NOT NULL, runtime_seconds REAL NOT NULL,
 request_budget INTEGER NOT NULL, time_budget_seconds REAL NOT NULL, counts_json TEXT NOT NULL,
 stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS phase6f_contact_channels (
 candidate_id TEXT NOT NULL, channel_type TEXT NOT NULL, email TEXT, channel_url TEXT NOT NULL,
 source_url TEXT NOT NULL, evidence TEXT NOT NULL, purpose TEXT, review_status TEXT NOT NULL DEFAULT 'PENDING',
 PRIMARY KEY(candidate_id, channel_type, channel_url)
);
"""
ID_FIELDS = ("candidate_id", "company_name", "website", "canonical_domain", "industry", "location", "source_name", "source_url", "source_record_id", "source_license_or_permission", "verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "verification_reason", "qualification_evidence", "fetch_status", "http_status", "evidence_json", "last_inspected_at", "selection_rank", "selection_reason")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def initialize(path: Path = PHASE6F_DB, source: Path = PHASE6E_DB) -> None:
    """Make a one-time SQLite backup; existing Phase 6F state is never overwritten."""
    if not path.exists():
        if not source.exists():
            raise FileNotFoundError(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src, sqlite3.connect(path) as dst:
            src.backup(dst)
    with sqlite3.connect(path) as db:
        db.executescript(RUN_TABLES)


def select_pilot(limit: int = 500, *, shortlist: Path = TOP5K, db_path: Path = PHASE6E_DB) -> list[dict[str, Any]]:
    """Choose likely-fit OSM leads deterministically; exclude any previously attempted identity."""
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = _read_csv(shortlist)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        by_id = {row["candidate_id"]: dict(row) for row in db.execute("SELECT * FROM candidates")}
    eligible: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for row in rows:
        cid = row.get("candidate_id", "")
        source = by_id.get(cid)
        if not source or not cid.startswith("osm:") or source.get("verification_status") != "DISCOVERED_UNVERIFIED":
            continue
        # Quality-first deterministic ordering: strong first-pass business relevance,
        # no obvious chain/platform concern, existing Phase 6E score, then stable domain.
        likely_fit = row.get("screen_status") == "LIKELY_FIT"
        independent = row.get("chain_location_concern") == "NO_STRUCTURED_SIGNAL"
        clear_domain = row.get("website_domain_concern") == "NO_OBVIOUS_PATTERN"
        score = int(row.get("priority_score") or 0)
        key = (not likely_fit, not independent, not clear_domain, -score, source["canonical_domain"], cid)
        eligible.append((key, {**source, "selection_reason": "LIKELY_FIT first; independent/domain flags; descending Phase 6E priority; canonical-domain tie break"}))
    eligible.sort(key=lambda item: item[0])
    selected = [row for _, row in eligible[:limit]]
    if len(selected) < limit:
        raise ValueError(f"Only {len(selected)} unattempted OSM records are available; cannot select requested {limit}")
    return selected


def save_selection(rows: list[dict[str, Any]], *, db_path: Path = PHASE6F_DB, output: Path = OUT / "phase6f_selected_500.csv") -> None:
    initialize(db_path)
    stamp = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as db:
        for rank, row in enumerate(rows, 1):
            db.execute("INSERT OR IGNORE INTO phase6f_selection VALUES(?,?,?,?,?)", (row["candidate_id"], rank, row["selection_reason"], str(TOP5K.relative_to(ROOT)), stamp))
        db.commit()
    fields = ("selection_rank", "candidate_id", "company_name", "website", "canonical_domain", "industry", "location", "source_name", "source_url", "source_record_id", "screen_status", "priority_score", "chain_location_concern", "website_domain_concern", "selection_reason")
    # Stable selection order and bytes (timestamp is intentionally not exported).
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        exported = []
        for row in db.execute("SELECT s.selection_rank,s.candidate_id,c.* FROM phase6f_selection s JOIN candidates c USING(candidate_id) ORDER BY s.selection_rank"):
            item = dict(row)
            exported.append({key: item.get(key, "") for key in fields})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        # Screening metadata is read from source shortlist to preserve original evidence.
        meta = {r["candidate_id"]: r for r in _read_csv(TOP5K)}
        for item in exported:
            item.update({key: meta.get(item["candidate_id"], {}).get(key, "") for key in ("screen_status", "priority_score", "chain_location_concern", "website_domain_concern")})
            writer.writerow(item)


def _extract_verified_channels(db_path: Path) -> int:
    """Persist only existing parser output and same-domain public business channels."""
    inserted = 0
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        # Exclude individual contact details; this pilot retains only business-level channels.
        db.execute("DELETE FROM phase6f_contact_channels WHERE channel_type='NAMED_PERSON_EMAIL'")
        all_selected = db.execute("SELECT c.candidate_id,c.evidence_json FROM candidates c JOIN phase6f_selection s USING(candidate_id)").fetchall()
        for item in all_selected:
            try:
                evidence = json.loads(item["evidence_json"] or "{}")
            except json.JSONDecodeError:
                continue
            changed = False
            for page in evidence.get("pages", []):
                try:
                    raw_channels = json.loads(page.get("extracted_channels") or "[]")
                except json.JSONDecodeError:
                    raw_channels = []
                safe = []
                for channel in raw_channels if isinstance(raw_channels, list) else []:
                    kind = str(channel.get("type", "")).split(".")[-1]
                    if kind in {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "CONTACT_FORM"}:
                        safe.append({key: value for key, value in channel.items() if key not in {"name", "title"}})
                if "contact_evidence" in page or page.get("extracted_channels") != json.dumps(safe, sort_keys=True):
                    changed = True
                page.pop("contact_evidence", None)
                page["extracted_channels"] = json.dumps(safe, sort_keys=True)
            if changed:
                db.execute("UPDATE candidates SET evidence_json=? WHERE candidate_id=?", (json.dumps(evidence, sort_keys=True), item["candidate_id"]))
        candidates = db.execute("SELECT candidate_id,canonical_domain,verification_status,geo_eligibility_status,evidence_json FROM candidates WHERE candidate_id IN (SELECT candidate_id FROM phase6f_selection) AND verification_status='VERIFIED' AND geo_eligibility_status='ELIGIBLE'").fetchall()
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
                                cur = db.execute("INSERT OR IGNORE INTO phase6f_contact_channels(candidate_id,channel_type,email,channel_url,source_url,evidence,purpose) VALUES(?,?,?,?,?,?,?)",
                                                 (candidate["candidate_id"], "CONTACT_PAGE", None, page_url, page_url, "Official same-domain contact/business page was fetched by the existing bounded crawler", "Page URL indicates a contact or business-inquiry route"))
                                inserted += cur.rowcount
                        except ValueError:
                            pass
                try:
                    items = json.loads(page.get("extracted_channels") or "[]")
                except json.JSONDecodeError:
                    items = []
                for item in items:
                    kind = str(item.get("type", "")).split(".")[-1]
                    email = (item.get("email") or "").strip().casefold() or None
                    url = (item.get("url") or "").strip()
                    source_url = (item.get("source_url") or page.get("final_url") or page.get("requested_url") or "").strip()
                    if not url or not source_url:
                        continue
                    if kind not in {"PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "CONTACT_FORM"}:
                        continue
                    try:
                        expected = candidate["canonical_domain"]
                        if normalize_domain(source_url) != expected or normalize_domain(url) != expected:
                            continue
                        if email and normalize_domain("https://" + email.rsplit("@", 1)[-1]) != expected:
                            continue
                        # Existing public evidence; no pattern guessing, form submission, or approval.
                        cur = db.execute("INSERT OR IGNORE INTO phase6f_contact_channels(candidate_id,channel_type,email,channel_url,source_url,evidence,purpose) VALUES(?,?,?,?,?,?,?)",
                                         (candidate["candidate_id"], kind, email, url, source_url, str(item.get("evidence") or "")[:1200], str(item.get("purpose") or "")[:500]))
                        inserted += cur.rowcount
                    except ValueError:
                        continue
        db.execute("UPDATE candidates SET contact_status='CONTACT_FOUND' WHERE candidate_id IN (SELECT DISTINCT candidate_id FROM phase6f_contact_channels)")
        db.execute("UPDATE candidates SET contact_status='NOT_DISCOVERED' WHERE candidate_id IN (SELECT candidate_id FROM phase6f_selection) AND candidate_id NOT IN (SELECT DISTINCT candidate_id FROM phase6f_contact_channels) AND contact_status='CONTACT_FOUND'")
        db.commit()
    return inserted


def _run_totals(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        selected = db.execute("SELECT COUNT(*) FROM phase6f_selection").fetchone()[0]
        records = db.execute("SELECT c.verification_status,c.geo_eligibility_status,c.contact_status FROM candidates c JOIN phase6f_selection s USING(candidate_id)").fetchall()
        counts = Counter(row["verification_status"] for row in records)
        return {
            "selected": selected,
            "processed": sum(value for key, value in counts.items() if key != "DISCOVERED_UNVERIFIED"),
            "pending": counts.get("DISCOVERED_UNVERIFIED", 0),
            "verified": counts.get("VERIFIED", 0),
            "geo_eligible": sum(row["geo_eligibility_status"] == "ELIGIBLE" for row in records),
            "contact_candidates": sum(row["contact_status"] == "CONTACT_FOUND" for row in records),
            "verification_statuses": dict(sorted(counts.items())),
        }


def run_pilot(*, limit: int = 500, batch_size: int = 20, request_cap: int = 3000, time_cap_seconds: int = 10800,
              cost_cap_usd: float = 0.0, db_path: Path = PHASE6F_DB, shortlist: Path = TOP5K) -> dict[str, Any]:
    if not 1 <= batch_size <= 50 or request_cap < 1 or time_cap_seconds < 1:
        raise ValueError("batch_size must be 1..50 and request/time caps must be positive")
    if cost_cap_usd != 0:
        raise ValueError("Phase 6F supports a strict $0.00 paid-service cap; no paid API or LLM is used")
    initialize(db_path)
    with sqlite3.connect(db_path) as db:
        already = db.execute("SELECT COUNT(*) FROM phase6f_selection").fetchone()[0]
    if not already:
        selected = select_pilot(limit, shortlist=shortlist)
        save_selection(selected, db_path=db_path)
    elif already != limit:
        raise ValueError(f"Existing Phase 6F selection has {already} candidates, requested {limit}; resume with the original pilot size")
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    counters: Counter[str] = Counter()
    stop_reason = None
    with sqlite3.connect(db_path) as db:
        used_runtime = float(db.execute("SELECT COALESCE(SUM(runtime_seconds),0) FROM phase6f_runs").fetchone()[0])
    while True:
        elapsed = used_runtime + time.monotonic() - started
        if elapsed >= time_cap_seconds:
            stop_reason = "time_cap_exhausted"
            break
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            prior = db.execute("SELECT COALESCE(SUM(requests_attempted),0) FROM phase6f_runs").fetchone()
            used_requests = int(prior[0])
            if used_requests >= request_cap:
                stop_reason = "request_cap_exhausted"
                break
            todo = [r[0] for r in db.execute("SELECT c.candidate_id FROM candidates c JOIN phase6f_selection s USING(candidate_id) WHERE c.verification_status='DISCOVERED_UNVERIFIED' ORDER BY s.selection_rank LIMIT ?", (batch_size,))]
        if not todo:
            break
        remaining_time = max(1, time_cap_seconds - int(elapsed))
        batch_request_cap = min(200, request_cap - used_requests)
        result = verify_candidate_batch(len(todo), candidate_db=db_path, candidate_ids=todo,
                                       request_budget=batch_request_cap, time_budget_seconds=remaining_time,
                                       include_contact_evidence=True)
        _extract_verified_channels(db_path)
        counters.update({key: int(value) for key, value in result.get("counts", {}).items()})
        runtime = float(result.get("runtime_seconds", 0))
        stop_reason = "request_cap_exhausted" if result.get("request_budget_exhausted") and batch_request_cap >= request_cap - used_requests else None
        if result.get("time_budget_exhausted"):
            stop_reason = "time_cap_exhausted"
        finished_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT INTO phase6f_runs(started_at,finished_at,attempted,requests_attempted,requests_succeeded,requests_failed,retries,runtime_seconds,request_budget,time_budget_seconds,counts_json,stop_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (started_at, finished_at, result["attempted"], result["requests_attempted"], result["requests_succeeded"], result["requests_failed"], result["retries"], runtime, batch_request_cap, remaining_time, json.dumps(result.get("counts", {}), sort_keys=True), stop_reason))
            db.commit()
        if stop_reason:
            break
        if result["attempted"] == 0:
            stop_reason = "no_progress"
            break
    _extract_verified_channels(db_path)
    export_outputs(db_path=db_path, output_dir=OUT)
    report = pilot_report(db_path=db_path, stop_reason=stop_reason)
    (OUT / "phase6f_pilot_report.md").write_text(report, encoding="utf-8")
    totals = _run_totals(db_path)
    totals.update({"requests_attempted": 0, "requests_succeeded": 0, "requests_failed": 0, "retries": 0, "runtime_seconds_this_invocation": round(time.monotonic() - started, 2), "stop_reason": stop_reason})
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        for col in ("requests_attempted", "requests_succeeded", "requests_failed", "retries", "runtime_seconds"):
            totals[col] = db.execute(f"SELECT COALESCE(SUM({col}),0) FROM phase6f_runs").fetchone()[0]
    return totals


def export_outputs(*, db_path: Path = PHASE6F_DB, output_dir: Path = OUT) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        selected = [dict(row) for row in db.execute("SELECT c.*,s.selection_rank,s.selection_reason FROM candidates c JOIN phase6f_selection s USING(candidate_id) ORDER BY s.selection_rank")]
        channels = [dict(row) for row in db.execute("SELECT c.candidate_id,c.company_name,c.website,c.canonical_domain,c.source_name,c.source_url AS discovery_source_url,c.source_record_id,c.verification_status,c.geo_eligibility_status,ch.channel_type,ch.email,ch.channel_url,ch.source_url AS channel_evidence_url,ch.evidence,ch.purpose,ch.review_status FROM phase6f_contact_channels ch JOIN candidates c USING(candidate_id) JOIN phase6f_selection s USING(candidate_id) ORDER BY s.selection_rank,ch.channel_type,ch.channel_url")]
    def dump(filename: str, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> int:
        with (output_dir / filename).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})
        return len(rows)
    verified = [r for r in selected if r["verification_status"] == "VERIFIED"]
    geo = [r for r in verified if r["geo_eligibility_status"] == "ELIGIBLE"]
    failures = [r for r in selected if r["verification_status"] not in {"VERIFIED", "DISCOVERED_UNVERIFIED"}]
    base = (*ID_FIELDS,)
    counts = {
        "phase6f_verified_companies.csv": dump("phase6f_verified_companies.csv", verified, base),
        "phase6f_geo_eligible_companies.csv": dump("phase6f_geo_eligible_companies.csv", geo, base),
        "phase6f_contact_candidates.csv": dump("phase6f_contact_candidates.csv", channels, ("candidate_id","company_name","website","canonical_domain","source_name","discovery_source_url","source_record_id","verification_status","geo_eligibility_status","channel_type","email","channel_url","channel_evidence_url","evidence","purpose","review_status")),
        "phase6f_validation_failures.csv": dump("phase6f_validation_failures.csv", failures, base),
    }
    return counts


def pilot_report(*, db_path: Path = PHASE6F_DB, stop_reason: str | None = None) -> str:
    shortlist_ids = [row["candidate_id"] for row in _read_csv(TOP5K) if row.get("candidate_id", "").startswith("osm:")]
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        records = db.execute("SELECT c.*,s.selection_rank FROM candidates c JOIN phase6f_selection s USING(candidate_id) ORDER BY s.selection_rank").fetchall()
        runs = [dict(row) for row in db.execute("SELECT * FROM phase6f_runs ORDER BY run_id")]
        contact_count = db.execute("SELECT COUNT(DISTINCT candidate_id) FROM phase6f_contact_channels").fetchone()[0]
        channel_total = db.execute("SELECT COUNT(*) FROM phase6f_contact_channels").fetchone()[0]
        totals = _run_totals(db_path)
        identity = Counter(row["verification_status"] for row in records)
        eligible = Counter(row["geo_eligibility_status"] for row in records)
        processed = totals["processed"]
        requests = sum(row["requests_attempted"] for row in runs)
        success = identity.get("VERIFIED", 0)
        runtime = sum(row["runtime_seconds"] for row in runs)
        requests_per = requests / processed if processed else 0
        marks = ",".join("?" for _ in shortlist_ids)
        remaining = db.execute(f"SELECT COUNT(*) FROM candidates WHERE candidate_id IN ({marks}) AND verification_status='DISCOVERED_UNVERIFIED'", shortlist_ids).fetchone()[0] if shortlist_ids else 0
        return "\n".join([
            "# Phase 6F Website Verification and Contact Discovery Pilot", "",
            "## Scope and method", "",
            f"Selected {totals['selected']} OSM records from the Phase 6E Top 5,000 shortlist. Selection uses likely-fit first-pass category, independent-business/domain flags, descending Phase 6E priority, then canonical domain and candidate ID. Only still-unverified OSM records were eligible; previously verified and previously attempted statuses were excluded. The exact selection is recorded in `data/phase6f_selected_500.csv`.",
            "", "The pilot uses a one-time SQLite backup at `data/phase6f_candidates.db`; Phase 6E shortlist/database, Phase 6D source database, active cohorts, and campaign store are not written. The existing WebsiteCollector, identity verifier, and qualification rules are reused. Only verified + GEO-eligible records are passed to same-domain contact-channel evidence extraction. No guessed contacts, form submissions, outreach approvals, or sends are created.",
            "", "## Measured results", "",
            f"- Selected: {totals['selected']} | Processed: {processed} | Pending: {totals['pending']}",
            f"- Identity verified: {success} ({(100*success/processed if processed else 0):.1f}% of processed)",
            f"- GEO eligible: {totals['geo_eligible']} ({(100*totals['geo_eligible']/processed if processed else 0):.1f}% of processed)",
            f"- Companies with first-party contact channels: {contact_count} / {totals['geo_eligible']} GEO-eligible",
            f"- Public first-party contact channel records: {channel_total} (business email, contact form, or contact page; all PENDING review)",
            f"- HTTP requests: {requests}; successful responses: {sum(r['requests_succeeded'] for r in runs)}; failed responses: {sum(r['requests_failed'] for r in runs)}; retries: {sum(r['retries'] for r in runs)}",
            f"- Runtime: {runtime:.1f}s ({runtime/60:.1f} min); mean per processed candidate: {runtime/processed if processed else 0:.1f}s",
            "- Paid-service cost: $0.00 (hard cap; no paid API, LLM, or commercial verification service is used).",
            f"- Verification statuses: `{json.dumps(dict(sorted(identity.items())), sort_keys=True)}`",
            f"- GEO eligibility statuses: `{json.dumps(dict(sorted(eligible.items())), sort_keys=True)}`",
            f"- Stop reason: {stop_reason or 'selection complete or no pending selected candidates'}",
            "", "## Remaining work estimate", "",
            f"The Phase 6E Top 5,000 currently has {remaining:,} unverified OSM records outside completed outcomes (including any selected-but-pending records). At the observed {requests_per:.2f} HTTP requests and {runtime/processed if processed else 0:.1f} seconds per processed candidate, this remaining workload would require about {remaining*requests_per:,.0f} requests and {remaining*runtime/processed/3600 if processed else 0:.1f} hours. This is a linear extrapolation from this pilot, not a guarantee.",
            "", "## Outputs", "",
            "- `data/phase6f_verified_companies.csv` — verified identity and source/evidence status.",
            "- `data/phase6f_geo_eligible_companies.csv` — verified and eligible under existing rules.",
            "- `data/phase6f_contact_candidates.csv` — public, same-domain evidence; review status remains PENDING.",
            "- `data/phase6f_validation_failures.csv` — processed non-verified outcomes; unprocessed records are not called failures.",
            "- `data/phase6f_selected_500.csv` — deterministic workload selection.",
            "", "## Interpretation and limitations", "",
            "A VERIFIED outcome means the existing first-party identity matcher found sufficient name/domain/title/content alignment; it does not certify legal-entity status or independently prove that the business is currently trading. Because source records are OSM points of interest, a match may still identify a physical establishment/location rather than a distinct company; this distinction remains for human review. GEO eligibility is the existing ruleset applied only after identity verification. Current first-party site content, URLs, structured-data types, and qualification signals are retained for review, but website content alone is not an external operating-status check. Contact records are restricted to business-level emails, contact forms, and contact pages; individual names/personal email types are excluded, and all channel review states remain PENDING.",
            "", "A completed pilot means all 500 have a terminal verification status. Pending records remain explicit if a configured request/time cap stops execution. No contacts are outreach-approved.", ""
        ])


def integrity(path: Path = PHASE6F_DB) -> str:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def dashboard_summary(path: Path = PHASE6F_DB) -> dict[str, int]:
    if not path.exists():
        return {"selected": 0, "processed": 0, "verified": 0, "geo_eligible": 0, "contact_channels_found": 0, "pending_verification": 0, "review_required": 0}
    totals = _run_totals(path)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        contact_channels = db.execute("SELECT COUNT(*) FROM phase6f_contact_channels").fetchone()[0]
        # Every discovered contact remains pending human review; unresolved identity or qualification also needs review.
        pending_reviews = db.execute("SELECT COUNT(*) FROM candidates c JOIN phase6f_selection s USING(candidate_id) WHERE c.human_review_status='PENDING' AND c.verification_status<>'DISCOVERED_UNVERIFIED'").fetchone()[0]
    return {"selected": totals["selected"], "processed": totals["processed"], "verified": totals["verified"], "geo_eligible": totals["geo_eligible"], "contact_channels_found": contact_channels, "pending_verification": totals["pending"], "review_required": pending_reviews}


def dashboard_overlays(candidate_ids: list[str], path: Path = PHASE6F_DB) -> dict[str, dict[str, Any]]:
    if not path.exists() or not candidate_ids:
        return {}
    marks = ",".join("?" for _ in candidate_ids)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT c.candidate_id,c.verification_status,c.geo_eligibility_status,c.geo_opportunity_status,c.contact_status,c.verification_reason,c.last_inspected_at FROM candidates c JOIN phase6f_selection s USING(candidate_id) WHERE c.candidate_id IN ({marks})", candidate_ids).fetchall()
    return {row["candidate_id"]: dict(row) for row in rows}
