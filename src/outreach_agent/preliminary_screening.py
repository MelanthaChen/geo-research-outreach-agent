"""Bounded, resumable pre-consent website screening for the Research Queue.

This module is intentionally independent of the GEO Engine audit/experiment
pipeline.  It writes only to the Outreach Agent lifecycle registry.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import Request, urlopen

from outreach_agent.research_lifecycle import REGISTRY_DB, init_registry, now


SCREENING_VERSION = "preliminary-screening/v1"
DEFAULT_USER_AGENT = "GEOResearchOutreachPreScreen/1.0 (+research contact)"
EMAIL_TYPES = {"GENERAL_BUSINESS_EMAIL", "SALES_MARKETING_EMAIL", "PARTNERSHIP_EMAIL", "BUSINESS_DEVELOPMENT_EMAIL", "NAMED_PERSON_EMAIL"}


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self._title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"p", "h1", "h2", "h3", "li", "title", "meta"}:
            self.parts.append(" ")
        if tag == "title":
            self._title = True
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.parts.append(text)
            if self._title and not self.title:
                self.title = text


@dataclass(frozen=True)
class ScreeningConfig:
    max_pages: int = 3
    timeout_seconds: float = 8.0
    per_domain_delay_seconds: float = 1.0
    max_content_chars: int = 12000
    max_retries: int = 2
    global_concurrency: int = 1
    request_budget: int = 100


class ScreeningProvider:
    name = "disabled"
    model = "none"
    prompt_version = "none"

    def interpret(self, *, name: str, industry: str | None, pages: list[dict[str, str]]) -> list[dict[str, str]]:
        return []


class MockScreeningProvider(ScreeningProvider):
    name = "mock"
    model = "deterministic-mock"
    prompt_version = "preliminary-mock-v1"

    def interpret(self, *, name: str, industry: str | None, pages: list[dict[str, str]]) -> list[dict[str, str]]:
        text = " ".join(page.get("text", "") for page in pages)
        if not text:
            return []
        subject = industry or "service"
        return [
            {"title": f"Clarify {subject} services", "claim": "Preliminary hypothesis: make the primary services and audience easier to identify in public website text."},
            {"title": "Strengthen entity context", "claim": "Preliminary hypothesis: connect the organization name, location, and service descriptions consistently across relevant pages."},
        ][:2]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _schema(path: Path) -> None:
    init_registry(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(research_records)")}
        additions = {
            "readiness_reason_json": "TEXT NOT NULL DEFAULT '[]'",
            "screening_provider": "TEXT",
            "screening_model": "TEXT",
            "screening_prompt_version": "TEXT",
            "screening_completed_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE research_records ADD COLUMN {name} {definition}")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS screening_jobs (
          job_id TEXT PRIMARY KEY, status TEXT NOT NULL, provider TEXT NOT NULL,
          dry_run INTEGER NOT NULL, max_candidates INTEGER NOT NULL, request_budget INTEGER NOT NULL,
          created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, processed INTEGER NOT NULL DEFAULT 0,
          succeeded INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, error_summary_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS screening_queue (
          job_id TEXT NOT NULL, profile_id TEXT NOT NULL, status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_error TEXT,
          PRIMARY KEY(job_id, profile_id)
        );
        CREATE TABLE IF NOT EXISTS screening_pages (
          screening_id TEXT NOT NULL, profile_id TEXT NOT NULL, url TEXT NOT NULL,
          fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL, http_status INTEGER,
          excerpt TEXT NOT NULL, deterministic_checks_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(screening_id, url)
        );
        CREATE INDEX IF NOT EXISTS screening_queue_status_idx ON screening_queue(status, next_attempt_at);
        """)


def _channels(row: sqlite3.Row | dict) -> list[dict]:
    try:
        values = json.loads(row["contact_channels_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in values if str(item.get("review_status", "PENDING")).upper() != "REJECTED"]


def _usable_channels(row: sqlite3.Row | dict) -> list[dict]:
    result = []
    for item in _channels(row):
        kind = str(item.get("channel_type") or "").upper()
        if item.get("email") and kind in EMAIL_TYPES:
            result.append(item)
        elif kind == "CONTACT_FORM" and item.get("channel_url"):
            result.append(item)
    return result


def audit_readiness(path: Path = REGISTRY_DB) -> dict[str, object]:
    """Return overlapping blocker counts without changing any records."""
    _schema(path)
    counts: Counter[str] = Counter()
    by_type: Counter[tuple[str, str]] = Counter()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM research_records WHERE profile_type <> 'owner_controlled_test'").fetchall()
    for row in rows:
        reasons: list[str] = []
        if not row["official_website"]: reasons.append("MISSING_WEBSITE")
        if row["identity_verification_status"] != "VERIFIED": reasons.append("UNVERIFIED_COMPANY_IDENTITY")
        if row["qualification_status"] != "ELIGIBLE": reasons.append("NOT_ELIGIBLE_OR_NEEDS_REVIEW")
        # Company size is a soft prioritization signal, not a screening gate.
        channels = _usable_channels(row)
        if not channels: reasons.append("MISSING_USABLE_CONTACT_CHANNEL")
        if channels and not any(item.get("email") for item in channels): reasons.append("FORM_ONLY_NO_EMAIL")
        if not row["provenance_json"] or row["provenance_json"] in {"[]", "{}"}: reasons.append("MISSING_PROVENANCE")
        if row["consent_status"] in {"DECLINED", "WITHDRAWN"} or row["recruitment_status"] == "EXCLUDED": reasons.append("SUPPRESSED_OR_EXCLUDED")
        if row["screening_status"] not in {"COMPLETE", "SCREENING_COMPLETE"}: reasons.append("MISSING_PRELIMINARY_SCREENING")
        for reason in reasons: counts[reason] += 1
        for reason in reasons: by_type[(row["profile_type"], reason)] += 1
    return {"total": len(rows), "reason_counts": dict(sorted(counts.items())), "by_profile_type": {f"{kind}:{reason}": count for (kind, reason), count in sorted(by_type.items())}}


def select_candidates(path: Path = REGISTRY_DB, *, limit: int = 25, profile_type: str = "") -> list[str]:
    if not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")
    _schema(path)
    where = ["identity_verification_status='VERIFIED'", "qualification_status='ELIGIBLE'", "official_website <> ''", "screening_status NOT IN ('COMPLETE','SCREENING_COMPLETE')", "profile_type <> 'owner_controlled_test'"]
    args: list[str] = []
    if profile_type:
        where.append("profile_type=?"); args.append(profile_type)
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute("SELECT profile_id FROM research_records WHERE " + " AND ".join(where) + " ORDER BY updated_at,profile_id LIMIT ?", args + [limit])]


def _fetch(url: str, config: ScreeningConfig, *, mock: bool, name: str, industry: str | None) -> tuple[list[dict[str, str]], dict[str, str]]:
    if mock:
        text = f"{name}. Public website information about {industry or 'services'} and how customers can contact the organization."
        return [{"url": url, "text": text, "title": name}], {"robots": "MOCK_ALLOWED", "mode": "MOCK"}
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid official website URL")
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    robots = RobotFileParser(robots_url); robots.set_url(robots_url)
    try: robots.read()
    except Exception as exc: raise RuntimeError(f"ROBOTS_UNAVAILABLE:{type(exc).__name__}") from exc
    if not robots.can_fetch(DEFAULT_USER_AGENT, url): raise PermissionError("ROBOTS_DENIED")
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html"})
    with urlopen(request, timeout=config.timeout_seconds) as response:
        body = response.read(config.max_content_chars * 4)
        final_url = response.geturl()
        charset = response.headers.get_content_charset() or "utf-8"
        html = body.decode(charset, errors="replace")
        parser = PageParser(); parser.feed(html)
        text = " ".join(parser.parts)[:config.max_content_chars]
        return [{"url": final_url, "text": text, "title": parser.title}], {"robots": "ALLOWED", "mode": "LIVE", "http_status": str(response.status)}


def plan_job(path: Path = REGISTRY_DB, *, limit: int = 10, provider: str = "mock", dry_run: bool = True, request_budget: int = 25) -> str:
    _schema(path)
    ids = select_candidates(path, limit=limit)
    job_id = "screen-job-" + secrets.token_urlsafe(9)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO screening_jobs(job_id,status,provider,dry_run,max_candidates,request_budget,created_at) VALUES (?,?,?,?,?,?,?)", (job_id, "PLANNED", provider, int(dry_run), limit, request_budget, now()))
        db.executemany("INSERT INTO screening_queue(job_id,profile_id,status) VALUES (?,?,?)", [(job_id, profile_id, "PENDING") for profile_id in ids])
    return job_id


def run_job(job_id: str, path: Path = REGISTRY_DB, *, provider: ScreeningProvider | None = None, config: ScreeningConfig | None = None, mock_fetch: bool = True, pause_after: int | None = None) -> dict[str, object]:
    _schema(path); config = config or ScreeningConfig(); provider = provider or MockScreeningProvider()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        job = db.execute("SELECT * FROM screening_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job: raise KeyError(job_id)
        db.execute("UPDATE screening_jobs SET status='RUNNING',started_at=? WHERE job_id=? AND status IN ('PLANNED','PAUSED','RUNNING')", (now(), job_id))
        queue = db.execute("SELECT q.*,r.name,r.industry,r.official_website,r.profile_version FROM screening_queue q JOIN research_records r USING(profile_id) WHERE q.job_id=? AND q.status IN ('PENDING','RETRY') ORDER BY q.profile_id", (job_id,)).fetchall()
        processed = 0; errors: Counter[str] = Counter()
        for item in queue:
            if pause_after is not None and processed >= pause_after:
                db.execute("UPDATE screening_jobs SET status='PAUSED' WHERE job_id=?", (job_id,)); break
            try:
                pages, meta = _fetch(item["official_website"], config, mock=mock_fetch, name=item["name"], industry=item["industry"])
                opportunities = provider.interpret(name=item["name"], industry=item["industry"], pages=pages)
                evidence = [{"url": page["url"], "excerpt": page["text"][:600], "captured_at": now(), "content_hash": _hash(page["text"]), "deterministic_checks": meta} for page in pages]
                screening_id = "screening-" + _hash(item["profile_id"] + _hash(json.dumps(evidence, sort_keys=True)))[:20]
                db.execute("INSERT OR IGNORE INTO screenings VALUES (?,?,?,?,?,?,?,?,?,?,?)", (screening_id,item["profile_id"],item["profile_version"],"PRELIMINARY_UNVALIDATED",json.dumps(opportunities,sort_keys=True),json.dumps(evidence,sort_keys=True),provider.name,provider.model,provider.prompt_version,SCREENING_VERSION,now()))
                for page in evidence:
                    db.execute("INSERT OR REPLACE INTO screening_pages VALUES (?,?,?,?,?,?,?,?)", (screening_id,item["profile_id"],page["url"],page["captured_at"],page["content_hash"],int(meta.get("http_status", "0")) if meta.get("http_status", "0").isdigit() else None,page["excerpt"],json.dumps(page["deterministic_checks"],sort_keys=True)))
                row = db.execute("SELECT * FROM research_records WHERE profile_id=?", (item["profile_id"],)).fetchone()
                readiness, reasons = calculate_readiness(row, opportunities)
                db.execute("UPDATE research_records SET screening_status='SCREENING_COMPLETE',screening_provider=?,screening_model=?,screening_prompt_version=?,screening_completed_at=?,outreach_ready_status=?,readiness_reason_json=?,updated_at=? WHERE profile_id=?", (provider.name,provider.model,provider.prompt_version,now(),readiness,json.dumps(reasons,sort_keys=True),now(),item["profile_id"]))
                db.execute("UPDATE screening_queue SET status='COMPLETE',attempts=attempts+1 WHERE job_id=? AND profile_id=?", (job_id,item["profile_id"]))
            except Exception as exc:
                category = str(exc).split(":",1)[0] or type(exc).__name__; errors[category] += 1
                db.execute("UPDATE screening_queue SET status='FAILED',attempts=attempts+1,last_error=? WHERE job_id=? AND profile_id=?", (f"{category}:{exc}",job_id,item["profile_id"]))
                db.execute("UPDATE research_records SET screening_status='SCREENING_FAILED',readiness_reason_json=?,updated_at=? WHERE profile_id=?", (json.dumps(["SCREENING_FAILED",category]),now(),item["profile_id"]))
            processed += 1
        remaining = db.execute("SELECT count(*) FROM screening_queue WHERE job_id=? AND status IN ('PENDING','RETRY')", (job_id,)).fetchone()[0]
        status = "PAUSED" if pause_after is not None and processed < len(queue) else ("COMPLETE" if remaining == 0 else "RUNNING")
        db.execute("UPDATE screening_jobs SET status=?,completed_at=?,processed=processed+?,succeeded=succeeded+?,failed=failed+?,error_summary_json=? WHERE job_id=?", (status, now() if status == "COMPLETE" else None, processed, processed-len(errors), len(errors), json.dumps(dict(errors),sort_keys=True), job_id))
    return {"job_id": job_id, "status": status, "processed": processed, "remaining": remaining, "errors": dict(errors), "provider": provider.name, "mock_fetch": mock_fetch}


def calculate_readiness(row: sqlite3.Row | dict, opportunities: list[dict]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if row["profile_type"] == "owner_controlled_test": return "NOT_ELIGIBLE", ["OWNER_CONTROLLED_TEST"]
    if not row["official_website"]: reasons.append("MISSING_WEBSITE")
    if row["identity_verification_status"] != "VERIFIED": reasons.append("UNVERIFIED_COMPANY_IDENTITY")
    if row["qualification_status"] != "ELIGIBLE": reasons.append("NOT_ELIGIBLE_OR_NEEDS_REVIEW")
    # Company size remains available for ordering but is not required here.
    channels = _usable_channels(row)
    if not channels: reasons.append("MISSING_USABLE_CONTACT_CHANNEL")
    if not row["provenance_json"] or row["provenance_json"] in {"[]", "{}"}: reasons.append("MISSING_PROVENANCE")
    if row["consent_status"] in {"DECLINED", "WITHDRAWN"} or row["recruitment_status"] == "EXCLUDED": reasons.append("SUPPRESSED_OR_EXCLUDED")
    if not opportunities: reasons.append("NO_EVIDENCE_BACKED_OPPORTUNITY")
    if reasons:
        hard = {"MISSING_WEBSITE","UNVERIFIED_COMPANY_IDENTITY","NOT_ELIGIBLE_OR_NEEDS_REVIEW","SUPPRESSED_OR_EXCLUDED"}
        return ("NOT_ELIGIBLE" if any(reason in hard for reason in reasons) else "NEEDS_REVIEW"), reasons
    return ("READY_FOR_EMAIL" if any(item.get("email") for item in channels) else "READY_FOR_MANUAL_CONTACT"), []
