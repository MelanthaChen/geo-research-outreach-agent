"""Persistent, local-only DEMO campaign state.

Campaigns deliberately live in a sidecar database. The Phase 4F company cohort
and its qualification/contact review fields are only read by the caller.
There is no delivery provider in this module: every queue transition is marked
SIMULATED and only changes this isolated demo store.
"""

from __future__ import annotations

import hashlib
import csv
import io
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from outreach_agent.dashboard_data import make_draft_preview
from outreach_agent.outreach_pipeline import SuppressionRegistry, validate_recipient

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CAMPAIGN_DB = ROOT / "data/phase5d_campaigns.db"
MAX_BATCH_SIZE = 10
MAX_CAMPAIGN_COMPANIES = 5000
TOKEN_TTL_DAYS = 30
FORM_CHOICES = {"INTERESTED", "MORE_INFO", "NOT_INTERESTED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CampaignStore:
    """SQLite persistence isolated from both company databases."""

    def __init__(self, path: Path = DEFAULT_CAMPAIGN_DB, suppression_path: Path | None = None):
        self.path = Path(path)
        self.suppression_path = Path(suppression_path or ROOT / "data/outreach_suppression.csv")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    demo_only INTEGER NOT NULL DEFAULT 1 CHECK(demo_only=1),
                    batch_size INTEGER NOT NULL DEFAULT 10 CHECK(batch_size BETWEEN 1 AND 10),
                    daily_cap INTEGER NOT NULL DEFAULT 50 CHECK(daily_cap BETWEEN 1 AND 50),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    approved_at TEXT, cancelled_at TEXT, emergency_stopped INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS campaign_items (
                    id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    company_id INTEGER NOT NULL, company_name TEXT NOT NULL, website TEXT NOT NULL DEFAULT '',
                    contact_id INTEGER, channel_type TEXT NOT NULL DEFAULT '', recipient TEXT NOT NULL DEFAULT '',
                    evidence_url TEXT NOT NULL DEFAULT '', qualification_status TEXT NOT NULL,
                    identity_status TEXT NOT NULL, contact_review_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    item_status TEXT NOT NULL, draft_json TEXT, form_url TEXT NOT NULL DEFAULT '',
                    token_hash TEXT UNIQUE, token_expires_at TEXT, token_revoked INTEGER NOT NULL DEFAULT 0,
                    form_opened_at TEXT,
                    idempotency_key TEXT UNIQUE, delivery_status TEXT NOT NULL DEFAULT 'NOT_SENT',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL DEFAULT 'DEMO',
                    provider_message_id TEXT NOT NULL DEFAULT '', response_status TEXT NOT NULL DEFAULT '',
                    interest_status TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(campaign_id, company_id)
                );
                CREATE INDEX IF NOT EXISTS ix_campaign_items_company ON campaign_items(company_id, created_at);
                CREATE INDEX IF NOT EXISTS ix_campaign_items_queue ON campaign_items(campaign_id, delivery_status, id);
                CREATE TABLE IF NOT EXISTS demo_interest_submissions (
                    id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL UNIQUE REFERENCES campaign_items(id) ON DELETE CASCADE,
                    company_id INTEGER NOT NULL, campaign_id TEXT NOT NULL, interest TEXT NOT NULL,
                    form_data_json TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'PENDING_RESEARCH_TEAM_REVIEW',
                    is_simulated INTEGER NOT NULL DEFAULT 1 CHECK(is_simulated=1),
                    submitted_at TEXT NOT NULL, reviewed_at TEXT, handoff_status TEXT NOT NULL DEFAULT 'NOT_STARTED'
                );
                CREATE TABLE IF NOT EXISTS campaign_audit (
                    id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    item_id INTEGER, event_type TEXT NOT NULL, details_json TEXT NOT NULL,
                    actor_label TEXT NOT NULL DEFAULT 'LOCAL_DEMO_USER', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS demo_control (
                    id INTEGER PRIMARY KEY CHECK(id=1), emergency_stopped INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                INSERT OR IGNORE INTO demo_control(id,emergency_stopped,updated_at) VALUES(1,0,CURRENT_TIMESTAMP);
                """
            )
            item_columns = {r["name"] for r in db.execute("PRAGMA table_info(campaign_items)")}
            if "form_opened_at" not in item_columns:
                db.execute("ALTER TABLE campaign_items ADD COLUMN form_opened_at TEXT")
            campaign_columns = {r["name"] for r in db.execute("PRAGMA table_info(campaigns)")}
            if "daily_cap" not in campaign_columns:
                db.execute("ALTER TABLE campaigns ADD COLUMN daily_cap INTEGER NOT NULL DEFAULT 50")

    @staticmethod
    def _audit(db: sqlite3.Connection, campaign_id: str, event: str,
               details: dict[str, Any] | None = None, item_id: int | None = None) -> None:
        db.execute(
            "INSERT INTO campaign_audit(campaign_id,item_id,event_type,details_json,created_at) VALUES(?,?,?,?,?)",
            (campaign_id, item_id, event, json.dumps(details or {}, sort_keys=True), _now()),
        )

    def create(self, data: dict[str, Any], name: str, company_ids: list[int]) -> dict[str, Any]:
        label = " ".join(str(name).split())[:120]
        if not label:
            raise ValueError("Campaign name is required")
        if not isinstance(company_ids, list):
            raise ValueError("company_ids must be a list of stable cohort IDs")
        ids = list(dict.fromkeys(int(value) for value in company_ids))
        if not ids or len(ids) > min(len(data["companies"]), MAX_CAMPAIGN_COMPANIES):
            raise ValueError(f"Select between 1 and {min(len(data['companies']), MAX_CAMPAIGN_COMPANIES)} companies")
        unknown_ids = sorted(set(ids) - set(data["details"]))
        if unknown_ids:
            raise ValueError(f"Unknown company IDs: {unknown_ids[:10]}")
        campaign_id = str(uuid.uuid4())
        now = _now()
        suppression = SuppressionRegistry.load(self.suppression_path)
        with self.connect() as db:
            db.execute(
                "INSERT INTO campaigns(id,name,status,created_at,updated_at) VALUES(?,?,'DRAFT',?,?)",
                (campaign_id, label, now, now),
            )
            for company_id in ids:
                detail = data["details"].get(company_id)
                if not detail:
                    continue
                eligibility = str(detail.get("eligibility") or "NEEDS_REVIEW").upper()
                identity = str(detail.get("identity_verification_status") or "UNKNOWN").upper()
                suitable = [c for c in detail.get("contact_channels", []) if c.get("is_suitable_first_party")]
                emails = [c for c in suitable if c.get("channel_type") != "CONTACT_FORM"]
                forms = [c for c in suitable if c.get("channel_type") == "CONTACT_FORM"]
                chosen = None
                status = ""
                recipient = ""
                if eligibility != "ELIGIBLE":
                    status = "EXCLUDED_NOT_ELIGIBLE"
                elif identity != "VERIFIED":
                    status = "EXCLUDED_IDENTITY_UNVERIFIED"
                elif not suitable:
                    status = "EXCLUDED_NO_SUITABLE_CHANNEL"
                elif emails:
                    chosen = sorted(emails, key=lambda c: (c.get("review_status") != "APPROVED", int(c["id"])))[0]
                    # A prior simulated delivery suppresses duplicate invitations across campaigns.
                    prior = db.execute(
                        "SELECT 1 FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE i.company_id=? AND (i.delivery_status='SIMULATED_DELIVERED' OR (c.status<>'CANCELLED' AND i.item_status IN ('DRAFT_REVIEW','CONTACT_FORM_REVIEW'))) LIMIT 1",
                        (company_id,),
                    ).fetchone()
                    if prior:
                        status = "EXCLUDED_ALREADY_CAMPAIGNED"
                    else:
                        try:
                            recipient = validate_recipient(chosen.get("email"))
                        except ValueError:
                            status = "EXCLUDED_DRAFT_VALIDATION"
                        else:
                            status = "EXCLUDED_SUPPRESSED_OPT_OUT" if suppression.is_suppressed(recipient) else "DRAFT_REVIEW"
                else:
                    chosen = sorted(forms, key=lambda c: int(c["id"]))[0]
                    status = "CONTACT_FORM_REVIEW"
                    prior = db.execute(
                        "SELECT 1 FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE i.company_id=? AND (i.delivery_status='SIMULATED_DELIVERED' OR (c.status<>'CANCELLED' AND i.item_status IN ('DRAFT_REVIEW','CONTACT_FORM_REVIEW'))) LIMIT 1",
                        (company_id,),
                    ).fetchone()
                    if prior:
                        status = "EXCLUDED_ALREADY_CAMPAIGNED"

                draft: dict[str, Any] | None = None
                form_url = ""
                token_hash = ""
                expires = ""
                key = ""
                if chosen and status in {"DRAFT_REVIEW", "CONTACT_FORM_REVIEW"}:
                    try:
                        draft = make_draft_preview(data, company_id, int(chosen["id"]))
                    except (KeyError, ValueError, OSError):
                        status, draft = "EXCLUDED_DRAFT_VALIDATION", None
                    if draft is None:
                        pass
                    elif draft.get("status") != "DRAFT_READY":
                        status, chosen, draft = "EXCLUDED_DRAFT_VALIDATION", None, None
                    else:
                        token = ""
                        for _ in range(5):
                            candidate = secrets.token_urlsafe(32)
                            if not db.execute("SELECT 1 FROM campaign_items WHERE token_hash=?", (_token_hash(candidate),)).fetchone():
                                token = candidate
                                break
                        if not token:
                            status, draft, chosen = "EXCLUDED_DRAFT_VALIDATION", None, None
                    if draft is not None and chosen is not None:
                        form_url = f"https://participation.example.invalid/demo/{token}"
                        old_form_url = draft["participation_interest_form_url"]
                        draft["participation_interest_form_url"] = f"TEST LINK — NOT ACTIVE: {form_url}"
                        draft["body"] = draft["body"].replace(old_form_url, draft["participation_interest_form_url"])
                        draft["demo_form_token"] = token  # retained only inside this isolated local demo draft
                        token_hash = _token_hash(token)
                        expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat(timespec="seconds")
                        key_payload = f"{campaign_id}\0{company_id}\0{chosen['id']}\0{draft['subject']}\0{draft['body']}"
                        key = hashlib.sha256(key_payload.encode()).hexdigest()
                        if chosen.get("channel_type") == "CONTACT_FORM":
                            status = "CONTACT_FORM_REVIEW"
                db.execute(
                    """INSERT INTO campaign_items(
                       campaign_id,company_id,company_name,website,contact_id,channel_type,recipient,evidence_url,
                       qualification_status,identity_status,contact_review_status,item_status,draft_json,form_url,
                       token_hash,token_expires_at,idempotency_key,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (campaign_id, company_id, detail["company_name"], detail.get("website") or "",
                     int(chosen["id"]) if chosen else None, chosen.get("channel_type", "") if chosen else "",
                     recipient, chosen.get("source_url") or "" if chosen else "", eligibility, identity,
                     str(chosen.get("review_status") or "PENDING").upper() if chosen else "UNKNOWN", status,
                     json.dumps(draft, sort_keys=True) if draft else None, form_url, token_hash or None,
                     expires or None, key or None, now, now),
                )
            self._audit(db, campaign_id, "CAMPAIGN_CREATED", {"requested_company_count": len(ids), "demo_only": True})
        return self.get(campaign_id)

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM campaigns ORDER BY created_at DESC,id").fetchall()
            return [self._campaign_summary(db, row) for row in rows]

    @staticmethod
    def _campaign_summary(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        counts = {r["item_status"]: r["n"] for r in db.execute(
            "SELECT item_status,COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY item_status", (row["id"],)
        )}
        deliveries = {r["delivery_status"]: r["n"] for r in db.execute(
            "SELECT delivery_status,COUNT(*) n FROM campaign_items WHERE campaign_id=? GROUP BY delivery_status", (row["id"],)
        )}
        return {**dict(row), "item_counts": counts, "delivery_counts": deliveries,
                "company_count": sum(counts.values()),
                "draft_count": counts.get("DRAFT_REVIEW", 0) + counts.get("CONTACT_FORM_REVIEW", 0),
                "eligible_count": counts.get("DRAFT_REVIEW", 0) + counts.get("CONTACT_FORM_REVIEW", 0),
                "ineligible_count": counts.get("EXCLUDED_NOT_ELIGIBLE", 0),
                "unresolved_count": sum(counts.get(key, 0) for key in ("EXCLUDED_IDENTITY_UNVERIFIED", "EXCLUDED_NO_SUITABLE_CHANNEL", "EXCLUDED_DRAFT_VALIDATION")),
                "excluded_count": sum(n for key, n in counts.items() if key.startswith("EXCLUDED_")),
                "contact_form_review_count": counts.get("CONTACT_FORM_REVIEW", 0),
                "simulated_response_count": int(db.execute("SELECT COUNT(*) FROM campaign_items WHERE campaign_id=? AND response_status<>''", (row["id"],)).fetchone()[0]),
                "interest_submission_count": int(db.execute("SELECT COUNT(*) FROM demo_interest_submissions WHERE campaign_id=?", (row["id"],)).fetchone()[0]),
                "simulated_followup_count": int(db.execute("SELECT COUNT(*) FROM demo_interest_submissions WHERE campaign_id=? AND handoff_status<>'NOT_STARTED'", (row["id"],)).fetchone()[0]),
                "simulated_deliveries": deliveries.get("SIMULATED_DELIVERED", 0),
                "simulated_failures": int(db.execute("SELECT COUNT(*) FROM campaign_audit WHERE campaign_id=? AND event_type='SIMULATED_DELIVERY_RESULT' AND details_json LIKE '%FAILED%'", (row["id"],)).fetchone()[0]),
                "outstanding_failed_items": deliveries.get("SIMULATED_FAILED", 0)}

    def get(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as db:
            campaign = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            summary = self._campaign_summary(db, campaign)
            items = []
            for row in db.execute("SELECT * FROM campaign_items WHERE campaign_id=? ORDER BY company_name COLLATE NOCASE,company_id", (campaign_id,)):
                item = dict(row)
                item["draft"] = json.loads(item.pop("draft_json")) if item.get("draft_json") else None
                # API exposes the opaque token only inside the local-only simulator UI.
                item["demo_token"] = (item["draft"] or {}).get("demo_form_token", "")
                items.append(item)
            summary["items"] = items
            summary["audit"] = [dict(r) for r in db.execute("SELECT * FROM campaign_audit WHERE campaign_id=? ORDER BY id", (campaign_id,))]
            summary["interest_submissions"] = [dict(r) | {"form_data": json.loads(r["form_data_json"])} for r in db.execute(
                "SELECT * FROM demo_interest_submissions WHERE campaign_id=? ORDER BY id", (campaign_id,)
            )]
            return summary

    def approve_demo(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT status,approved_at,emergency_stopped FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                raise KeyError(campaign_id)
            if db.execute("SELECT emergency_stopped FROM demo_control WHERE id=1").fetchone()[0]:
                raise ValueError("Global test pause is active")
            if row["emergency_stopped"]:
                raise ValueError("Campaign test pause is active")
            if row["status"] not in {"DRAFT", "DEMO_APPROVED", "PAUSED"}:
                raise ValueError("Only a draft or paused campaign can be approved for test processing")
            now = _now()
            db.execute("UPDATE campaigns SET status='DEMO_APPROVED',approved_at=?,updated_at=? WHERE id=?", (now, now, campaign_id))
            self._audit(db, campaign_id, "DEMO_CAMPAIGN_APPROVED", {"scope": "DEMO_ONLY", "source_reviews_changed": False})
        return self.get(campaign_id)

    def set_status(self, campaign_id: str, status: str) -> dict[str, Any]:
        target = status.upper()
        allowed = {"PAUSED", "RESUMED", "CANCELLED"}
        if target not in allowed:
            raise ValueError("Status must be PAUSED, RESUMED, or CANCELLED")
        with self.connect() as db:
            row = db.execute("SELECT status,approved_at,emergency_stopped FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                raise KeyError(campaign_id)
            current = row["status"]
            if target == "PAUSED" and current not in {"RUNNING", "DEMO_APPROVED"}:
                raise ValueError("Campaign is not active")
            if target == "RESUMED" and current != "PAUSED":
                raise ValueError("Only a paused campaign can resume")
            if target == "RESUMED" and not row["approved_at"]:
                raise ValueError("Explicit approval for test processing is required before resuming")
            if target == "RESUMED" and row["emergency_stopped"]:
                raise ValueError("Clear the campaign emergency stop before resuming")
            if target == "RESUMED" and db.execute("SELECT emergency_stopped FROM demo_control WHERE id=1").fetchone()[0]:
                raise ValueError("Clear the global test pause before resuming")
            if target == "CANCELLED" and current == "CANCELLED":
                return self.get(campaign_id)
            mapped = {"PAUSED": "PAUSED", "RESUMED": "RUNNING", "CANCELLED": "CANCELLED"}[target]
            now = _now()
            db.execute("UPDATE campaigns SET status=?,updated_at=?,cancelled_at=? WHERE id=?",
                       (mapped, now, now if target == "CANCELLED" else None, campaign_id))
            if target == "CANCELLED":
                db.execute("UPDATE campaign_items SET token_revoked=1,updated_at=? WHERE campaign_id=?", (now, campaign_id))
            self._audit(db, campaign_id, f"CAMPAIGN_{target}", {"demo_only": True})
        return self.get(campaign_id)

    def emergency_stop(self, campaign_id: str) -> dict[str, Any]:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
                raise KeyError(campaign_id)
            now = _now()
            db.execute("UPDATE campaigns SET emergency_stopped=1,status='PAUSED',updated_at=? WHERE id=?", (now, campaign_id))
            db.execute("UPDATE campaign_items SET token_revoked=1,updated_at=? WHERE campaign_id=?", (now, campaign_id))
            self._audit(db, campaign_id, "EMERGENCY_STOP", {"demo_only": True, "form_tokens_revoked": True})
        return self.get(campaign_id)

    def global_emergency_stop(self, enabled: bool) -> dict[str, Any]:
        """Stop all local demo queues; clearing never resumes campaigns automatically."""
        with self.connect() as db:
            now = _now()
            db.execute("UPDATE demo_control SET emergency_stopped=?,updated_at=? WHERE id=1", (1 if enabled else 0, now))
            if enabled:
                db.execute("UPDATE campaigns SET emergency_stopped=1,status=CASE WHEN status IN ('DEMO_APPROVED','RUNNING','PAUSED') THEN 'PAUSED' ELSE status END,updated_at=?", (now,))
                db.execute("UPDATE campaign_items SET token_revoked=1,updated_at=?", (now,))
            else:
                db.execute("UPDATE campaigns SET emergency_stopped=0,updated_at=?", (now,))
            event = "GLOBAL_DEMO_EMERGENCY_STOP" if enabled else "GLOBAL_DEMO_EMERGENCY_STOP_CLEARED"
            for row in db.execute("SELECT id FROM campaigns").fetchall():
                self._audit(db, row["id"], event, {"enabled": enabled, "automatic_resume": False})
            return {"emergency_stopped": enabled, "campaigns_resumed": False, "real_sends": 0}

    def simulate_batch(self, campaign_id: str, limit: int = MAX_BATCH_SIZE,
                       outcomes: dict[int, str] | None = None) -> dict[str, Any]:
        batch_size = max(1, min(int(limit), MAX_BATCH_SIZE))
        outcomes = outcomes or {}
        with self.connect() as db:
            campaign = db.execute("SELECT status,batch_size,daily_cap,emergency_stopped,approved_at FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not campaign:
                raise KeyError(campaign_id)
            global_stop = db.execute("SELECT emergency_stopped FROM demo_control WHERE id=1").fetchone()[0]
            if global_stop or campaign["emergency_stopped"] or campaign["status"] in {"PAUSED", "CANCELLED"}:
                raise ValueError("Campaign is paused, cancelled, or stopped")
            if campaign["status"] not in {"DEMO_APPROVED", "RUNNING"}:
                raise ValueError("Campaign approval is required before test processing")
            if not campaign["approved_at"]:
                raise ValueError("Campaign approval is required before test processing")
            today_attempts = int(db.execute("SELECT COUNT(*) FROM campaign_audit WHERE campaign_id=? AND event_type='SIMULATED_DELIVERY_RESULT' AND substr(created_at,1,10)=substr(?,1,10)", (campaign_id, _now())).fetchone()[0])
            daily_budget = int(campaign["daily_cap"]) - today_attempts
            if daily_budget <= 0:
                raise ValueError("The campaign's local test-event limit has been reached")
            items = db.execute(
                "SELECT * FROM campaign_items WHERE campaign_id=? AND item_status='DRAFT_REVIEW' AND delivery_attempts<2 AND delivery_status IN ('NOT_SENT','SIMULATED_FAILED') ORDER BY id LIMIT ?",
                (campaign_id, min(batch_size, int(campaign["batch_size"]), daily_budget)),
            ).fetchall()
            now = _now()
            results = []
            for item in items:
                outcome = outcomes.get(int(item["company_id"]), "DELIVERED")
                if outcome not in {"DELIVERED", "FAILED"}:
                    raise ValueError("Demo outcome must be DELIVERED or FAILED")
                if outcome == "DELIVERED":
                    db.execute("UPDATE campaign_items SET delivery_status='SIMULATED_DELIVERED',delivery_attempts=delivery_attempts+1,provider='DEMO',provider_message_id=?,last_error='',updated_at=? WHERE id=?",
                               ("demo-" + secrets.token_hex(8), now, item["id"]))
                else:
                    db.execute("UPDATE campaign_items SET delivery_status='SIMULATED_FAILED',delivery_attempts=delivery_attempts+1,provider='DEMO',last_error='SIMULATED_FAILURE_FIXTURE',updated_at=? WHERE id=?",
                               (now, item["id"]))
                self._audit(db, campaign_id, "SIMULATED_DELIVERY_RESULT", {"result": outcome, "company_id": item["company_id"], "real_delivery": False}, int(item["id"]))
                results.append({"company_id": item["company_id"], "result": "SIMULATED_" + outcome})
            left = db.execute("SELECT COUNT(*) FROM campaign_items WHERE campaign_id=? AND item_status='DRAFT_REVIEW' AND delivery_attempts<2 AND delivery_status IN ('NOT_SENT','SIMULATED_FAILED')", (campaign_id,)).fetchone()[0]
            db.execute("UPDATE campaigns SET status=?,updated_at=? WHERE id=?", ("COMPLETED" if left == 0 else "RUNNING", now, campaign_id))
        return {"results": results, "processed": len(results), "batch_limit": batch_size, "real_sends": 0, "campaign": self.get(campaign_id)}

    def record_response(self, campaign_id: str, company_id: int, response: str) -> dict[str, Any]:
        response = response.upper()
        if response not in {"INTERESTED", "MORE_INFO", "DECLINED", "UNANSWERED", "OPTED_OUT"}:
            raise ValueError("Response must be INTERESTED, MORE_INFO, DECLINED, UNANSWERED, or OPTED_OUT")
        with self.connect() as db:
            item = db.execute("SELECT id,delivery_status FROM campaign_items WHERE campaign_id=? AND company_id=?", (campaign_id, company_id)).fetchone()
            if not item:
                raise KeyError(f"company {company_id} not found in campaign")
            if item["delivery_status"] != "SIMULATED_DELIVERED":
                raise ValueError("A test response requires a recorded test delivery")
            now = _now()
            db.execute("UPDATE campaign_items SET response_status=?,interest_status=?,updated_at=? WHERE id=?",
                       ("SIMULATED_" + response, "INTEREST_REPORTED" if response in {"INTERESTED", "MORE_INFO"} else "NO_INTEREST_RECORDED", now, item["id"]))
            self._audit(db, campaign_id, "SIMULATED_RECIPIENT_RESPONSE", {"response": response, "real_response": False, "suppression_registry_changed": False}, int(item["id"]))
        return self.get(campaign_id)

    def submit_demo_interest(self, token: str, values: dict[str, Any]) -> dict[str, Any]:
        token = str(token or "").strip()
        if not 32 <= len(token) <= 160:
            raise ValueError("Invalid or expired local test-form token")
        interest = str(values.get("interest") or "").upper()
        if interest not in FORM_CHOICES:
            raise ValueError("Choose Interested, More information, or Not interested")
        clean = {
            "company_name": str(values.get("company_name") or "").strip()[:200],
            "website": str(values.get("website") or "").strip()[:500],
            "contact_name": str(values.get("contact_name") or "").strip()[:200],
            "contact_role": str(values.get("contact_role") or "").strip()[:200],
            "business_email": validate_recipient(str(values.get("business_email") or "")),
            "interest": interest,
            "questions": str(values.get("questions") or "").strip()[:4000],
        }
        with self.connect() as db:
            item = db.execute("SELECT * FROM campaign_items WHERE token_hash=?", (_token_hash(token),)).fetchone()
            if not item or item["token_revoked"] or not item["token_expires_at"] or item["token_expires_at"] < _now():
                raise ValueError("Invalid, revoked, or expired local test-form token")
            if item["delivery_status"] != "SIMULATED_DELIVERED":
                raise ValueError("This test-form token is not associated with a recorded test delivery")
            prior = db.execute("SELECT id FROM demo_interest_submissions WHERE item_id=?", (item["id"],)).fetchone()
            if prior:
                return {"status": "DUPLICATE_IGNORED", "submission_id": int(prior["id"]), "simulated": True,
                        "company_id": item["company_id"], "message": "A local test submission already exists; no duplicate was stored."}
            now = _now()
            cursor = db.execute(
                "INSERT INTO demo_interest_submissions(item_id,company_id,campaign_id,interest,form_data_json,submitted_at) VALUES(?,?,?,?,?,?)",
                (item["id"], item["company_id"], item["campaign_id"], interest, json.dumps(clean, sort_keys=True), now),
            )
            db.execute("UPDATE campaign_items SET interest_status=?,updated_at=? WHERE id=?", ("SIMULATED_" + interest, now, item["id"]))
            self._audit(db, item["campaign_id"], "SIMULATED_INTEREST_FORM_SUBMITTED", {"formal_consent": False, "review_status": "PENDING_RESEARCH_TEAM_REVIEW"}, int(item["id"]))
            return {"status": "SIMULATED_SUBMISSION_RECORDED", "submission_id": int(cursor.lastrowid), "simulated": True,
                    "company_id": item["company_id"], "campaign_id": item["campaign_id"],
                    "review_status": "PENDING_RESEARCH_TEAM_REVIEW", "formal_consent": False}

    def open_demo_form(self, token: str) -> dict[str, Any]:
        if not 32 <= len(str(token or "")) <= 160:
            raise ValueError("Invalid or expired local test-form token")
        with self.connect() as db:
            item = db.execute("SELECT * FROM campaign_items WHERE token_hash=?", (_token_hash(token),)).fetchone()
            if not item or item["token_revoked"] or not item["token_expires_at"] or item["token_expires_at"] < _now():
                raise ValueError("Invalid, revoked, or expired local test-form token")
            if item["delivery_status"] != "SIMULATED_DELIVERED":
                raise ValueError("This test form is not associated with a recorded test delivery")
            now = _now()
            db.execute("UPDATE campaign_items SET form_opened_at=?,updated_at=? WHERE id=?", (now, now, item["id"]))
            self._audit(db, item["campaign_id"], "SIMULATED_FORM_OPENED", {"external_request": False}, int(item["id"]))
            return {"status": "SIMULATED_FORM_OPENED", "company_id": item["company_id"], "simulated": True,
                    "message": "Local test event only; no external form opened."}

    def review_demo_submission(self, submission_id: int) -> dict[str, Any]:
        with self.connect() as db:
            submission = db.execute("SELECT * FROM demo_interest_submissions WHERE id=?", (submission_id,)).fetchone()
            if not submission:
                raise KeyError(submission_id)
            now = _now()
            db.execute("UPDATE demo_interest_submissions SET review_status='DEMO_RESEARCH_TEAM_REVIEWED',reviewed_at=? WHERE id=?", (now, submission_id))
            self._audit(db, submission["campaign_id"], "DEMO_RESEARCH_TEAM_REVIEW_RECORDED", {"submission_id": submission_id, "formal_consent": False})
            return {"status": "DEMO_RESEARCH_TEAM_REVIEWED", "formal_consent": False, "simulated": True}

    def begin_demo_handoff(self, submission_id: int) -> dict[str, Any]:
        with self.connect() as db:
            submission = db.execute("SELECT * FROM demo_interest_submissions WHERE id=?", (submission_id,)).fetchone()
            if not submission:
                raise KeyError(submission_id)
            if submission["review_status"] != "DEMO_RESEARCH_TEAM_REVIEWED":
                raise ValueError("Research-team review is required before follow-up")
            if submission["interest"] == "NOT_INTERESTED":
                raise ValueError("A not-interested submission cannot continue the handoff")
            db.execute("UPDATE demo_interest_submissions SET handoff_status='SIMULATED_FOLLOWUP_STARTED' WHERE id=?", (submission_id,))
            self._audit(db, submission["campaign_id"], "SIMULATED_FOLLOWUP_RESEARCH_PROCESS_STARTED", {"submission_id": submission_id, "message_sent": False, "formal_consent": False})
            return {"status": "SIMULATED_FOLLOWUP_STARTED", "message_sent": False, "formal_consent": False, "simulated": True}

    def outreach_history(self, company_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT i.campaign_id,c.name campaign_name,i.item_status,i.channel_type,i.delivery_status,i.response_status,i.interest_status,i.provider,i.created_at,i.updated_at FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id WHERE i.company_id=? ORDER BY i.created_at DESC,i.id DESC",
                (company_id,),
            )]

    @staticmethod
    def _response_cte() -> str:
        return """
            WITH response_events AS (
                SELECT 'OUTCOME-' || i.id AS record_id, i.id AS item_id, i.company_id,
                       i.company_name, i.website, i.campaign_id, c.name AS campaign_name,
                       i.channel_type, i.recipient AS contact_email,
                       CASE upper(replace(i.response_status,'SIMULATED_',''))
                           WHEN 'INTERESTED' THEN 'INTERESTED'
                           WHEN 'MORE_INFO' THEN 'MORE_INFO'
                           WHEN 'DECLINED' THEN 'DECLINED'
                           WHEN 'OPTED_OUT' THEN 'OPTED_OUT'
                           ELSE 'UNKNOWN'
                       END AS status,
                       'TEST_RECIPIENT_OUTCOME' AS response_source, i.evidence_url AS source_evidence_url,
                       (SELECT a.created_at FROM campaign_audit a
                        WHERE a.item_id=i.id AND a.event_type='SIMULATED_RECIPIENT_RESPONSE'
                        ORDER BY a.id DESC LIMIT 1) AS activity_at,
                       'TEST' AS environment, i.response_status AS original_answer,
                       '' AS review_status, 0 AS formal_consent
                FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
                WHERE i.delivery_status='SIMULATED_DELIVERED'
                  AND i.response_status NOT IN ('','SIMULATED_UNANSWERED')
                UNION ALL
                SELECT 'FORM-' || s.id, i.id, i.company_id, i.company_name, i.website,
                       i.campaign_id, c.name, i.channel_type,
                       COALESCE(json_extract(s.form_data_json,'$.business_email'),i.recipient),
                       CASE
                           WHEN s.review_status='PENDING_RESEARCH_TEAM_REVIEW' THEN 'PENDING_VERIFICATION'
                           WHEN upper(s.interest)='INTERESTED' THEN 'INTERESTED'
                           WHEN upper(s.interest)='MORE_INFO' THEN 'MORE_INFO'
                           WHEN upper(s.interest)='NOT_INTERESTED' THEN 'DECLINED'
                           ELSE 'UNKNOWN'
                       END,
                       'PARTICIPATION_INTEREST_FORM', i.evidence_url, s.submitted_at, 'TEST',
                       s.interest, s.review_status, 0
                FROM demo_interest_submissions s
                JOIN campaign_items i ON i.id=s.item_id
                JOIN campaigns c ON c.id=s.campaign_id
                WHERE s.is_simulated=1
                UNION ALL
                SELECT 'NO-RESPONSE-' || i.id, i.id, i.company_id, i.company_name, i.website,
                       i.campaign_id, c.name, i.channel_type, i.recipient,
                       'NO_RESPONSE', '', i.evidence_url,
                       COALESCE(
                         (SELECT a.created_at FROM campaign_audit a
                          WHERE a.item_id=i.id AND a.event_type='SIMULATED_DELIVERY_RESULT'
                            AND a.details_json LIKE '%DELIVERED%'
                          ORDER BY a.id DESC LIMIT 1), i.updated_at, i.created_at),
                       'TEST', i.response_status, '', 0
                FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
                WHERE i.delivery_status='SIMULATED_DELIVERED'
                  AND i.response_status IN ('','SIMULATED_UNANSWERED')
                  AND NOT EXISTS (SELECT 1 FROM demo_interest_submissions s WHERE s.item_id=i.id)
            )
        """

    @staticmethod
    def _response_conditions(filters: dict[str, Any], *, include_status: bool = True) -> tuple[str, list[Any]]:
        clauses = ["1=1"]
        values: list[Any] = []
        status = str(filters.get("status") or "").upper()
        if include_status and status:
            if status == "NEEDS_FOLLOW_UP":
                clauses.append("status IN ('INTERESTED','MORE_INFO','PENDING_VERIFICATION')")
            else:
                clauses.append("status=?")
                values.append(status)
        if filters.get("campaign_id"):
            clauses.append("campaign_id=?")
            values.append(str(filters["campaign_id"]))
        if filters.get("source"):
            clauses.append("response_source=?")
            values.append(str(filters["source"]))
        environment = str(filters.get("environment") or "").upper()
        if environment:
            clauses.append("environment=?")
            values.append(environment)
        search = " ".join(str(filters.get("q") or "").split())
        if search:
            clauses.append("(instr(lower(company_name),lower(?))>0 OR instr(lower(website),lower(?))>0 OR instr(lower(contact_email),lower(?))>0)")
            values.extend((search, search, search))
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            date_value = str(filters.get(key) or "")
            if date_value:
                clauses.append(f"date(activity_at) {operator} date(?)")
                values.append(date_value)
        return " AND ".join(clauses), values

    @staticmethod
    def _response_order(sort: str) -> str:
        return {
            "oldest": "activity_at ASC, company_name COLLATE NOCASE ASC, record_id ASC",
            "company": "company_name COLLATE NOCASE ASC, activity_at DESC, record_id ASC",
            "status": "status ASC, activity_at DESC, company_name COLLATE NOCASE ASC, record_id ASC",
        }.get(sort, "activity_at DESC, company_name COLLATE NOCASE ASC, record_id ASC")

    def _response_counts(self, db: sqlite3.Connection, filters: dict[str, Any]) -> dict[str, int]:
        where, params = self._response_conditions(filters, include_status=False)
        counts = {row["status"]: int(row["n"]) for row in db.execute(
            f"{self._response_cte()} SELECT status,COUNT(*) n FROM response_events WHERE {where} GROUP BY status", params
        )}
        return {
            "INTERESTED": counts.get("INTERESTED", 0),
            "DECLINED": counts.get("DECLINED", 0),
            "NEEDS_FOLLOW_UP": sum(counts.get(status, 0) for status in ("INTERESTED", "MORE_INFO", "PENDING_VERIFICATION")),
            "NO_RESPONSE": counts.get("NO_RESPONSE", 0),
            **counts,
        }

    def response_page(self, filters: dict[str, Any] | None = None, *, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        filters = filters or {}
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        where, params = self._response_conditions(filters)
        order = self._response_order(str(filters.get("sort") or "recent"))
        with self.connect() as db:
            total = int(db.execute(f"{self._response_cte()} SELECT COUNT(*) FROM response_events WHERE {where}", params).fetchone()[0])
            rows = db.execute(
                f"{self._response_cte()} SELECT record_id,item_id,company_id,company_name,website,campaign_id,campaign_name,channel_type,status,response_source,source_evidence_url,activity_at,environment,original_answer,review_status,formal_consent FROM response_events WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            counts = self._response_counts(db, filters)
            sources = [row[0] for row in db.execute(
                f"{self._response_cte()} SELECT DISTINCT response_source FROM response_events WHERE response_source<>'' ORDER BY response_source"
            )]
            environments = [row[0] for row in db.execute(
                f"{self._response_cte()} SELECT DISTINCT environment FROM response_events ORDER BY environment"
            )]
        return {"records": [dict(row) for row in rows], "total": total, "page": page,
                "page_size": page_size, "counts": counts, "sources": sources,
                "environments": environments}

    def response_detail(self, record_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                f"{self._response_cte()} SELECT record_id,item_id,company_id,company_name,website,campaign_id,campaign_name,channel_type,status,response_source,source_evidence_url,activity_at,environment,original_answer,review_status,formal_consent FROM response_events WHERE record_id=?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise KeyError(record_id)
            result = dict(row)
        result["company_history"] = self.outreach_history(int(result["company_id"]))
        return result

    def export_responses_csv(self, filters: dict[str, Any] | None = None) -> str:
        filters = filters or {}
        where, params = self._response_conditions(filters)
        order = self._response_order(str(filters.get("sort") or "recent"))
        fields = ["record_id", "company_id", "company_name", "company_domain", "campaign_id", "campaign_name",
                  "contact_channel", "response_status", "response_source", "source_evidence_url", "activity_at", "environment",
                  "source_answer", "review_status", "formal_consent"]
        with self.connect() as db:
            rows = db.execute(
                f"{self._response_cte()} SELECT record_id,company_id,company_name,website,campaign_id,campaign_name,channel_type,status,response_source,source_evidence_url,activity_at,environment,original_answer,review_status,formal_consent FROM response_events WHERE {where} ORDER BY {order}",
                params,
            ).fetchall()
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            item = dict(row)
            host = urlparse(item.pop("website") or "").hostname or ""
            item["company_domain"] = host.casefold().removeprefix("www.")
            item["contact_channel"] = item.pop("channel_type")
            item["response_status"] = item.pop("status")
            item["source_answer"] = item.pop("original_answer")
            item = {key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value)
                    for key, value in item.items()}
            writer.writerow(item)
        return output.getvalue()

    def company_states(self) -> dict[int, str]:
        """One grouped query for Explorer campaign-state filtering."""
        with self.connect() as db:
            states: dict[int, str] = {}
            for row in db.execute("""SELECT company_id,
                MAX(CASE WHEN delivery_status='SIMULATED_DELIVERED' THEN 2
                         WHEN item_status IN ('DRAFT_REVIEW','CONTACT_FORM_REVIEW') THEN 1 ELSE 0 END) rank
                FROM campaign_items GROUP BY company_id"""):
                states[int(row["company_id"])] = {0: "NO_CAMPAIGN_HISTORY", 1: "DEMO_CAMPAIGNED", 2: "SIMULATED_DELIVERED"}[int(row["rank"])]
            return states

    def company_export_states(self) -> dict[int, dict[str, str]]:
        """Read-only campaign summary keyed by company for cohort exports."""
        with self.connect() as db:
            rows = db.execute(
                """SELECT i.company_id, c.name campaign_name,
                          MAX(CASE WHEN i.delivery_status='SIMULATED_DELIVERED' THEN
                              (SELECT MAX(a.created_at) FROM campaign_audit a
                               WHERE a.item_id=i.id AND a.event_type='SIMULATED_DELIVERY_RESULT')
                              ELSE '' END) last_delivery_at,
                          MAX(CASE WHEN i.delivery_status='SIMULATED_DELIVERED'
                                   THEN i.delivery_status ELSE '' END) delivery_status,
                          MAX(i.response_status) response_status,
                          MAX(i.interest_status) interest_status
                   FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
                   GROUP BY i.company_id,c.id ORDER BY i.company_id,c.name COLLATE NOCASE"""
            ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(int(row["company_id"]), {"campaigns": [], "dates": [], "statuses": [], "responses": [], "interests": []})
            item["campaigns"].append(str(row["campaign_name"]))
            if row["last_delivery_at"]:
                item["dates"].append(str(row["last_delivery_at"]))
            for column, key in (("delivery_status", "statuses"), ("response_status", "responses"), ("interest_status", "interests")):
                if row[column]:
                    item[key].append(str(row[column]))
        return {company_id: {
            "campaign_membership": " | ".join(sorted(set(item["campaigns"]), key=str.casefold)),
            "outreach_status": " | ".join(sorted(set(item["statuses"]))) or "CAMPAIGN_DRAFTED",
            "last_outreach_timestamp": max(item["dates"], default=""),
            "response_status": " | ".join(sorted(set(item["responses"]))),
            "interest_form_submission_status": " | ".join(sorted(set(item["interests"]))),
        } for company_id, item in grouped.items()}

    def analytics(self) -> dict[str, Any]:
        with self.connect() as db:
            statuses = {r["delivery_status"]: r["n"] for r in db.execute("SELECT delivery_status,COUNT(*) n FROM campaign_items GROUP BY delivery_status")}
            responses = {r["response_status"]: r["n"] for r in db.execute("SELECT response_status,COUNT(*) n FROM campaign_items WHERE response_status<>'' GROUP BY response_status")}
            interests = {r["interest"]: r["n"] for r in db.execute("SELECT interest,COUNT(*) n FROM demo_interest_submissions GROUP BY interest")}
            return {"campaigns": len(db.execute("SELECT 1 FROM campaigns").fetchall()), "company_items": int(db.execute("SELECT COUNT(*) FROM campaign_items").fetchone()[0]),
                    "drafts": int(db.execute("SELECT COUNT(*) FROM campaign_items WHERE draft_json IS NOT NULL").fetchone()[0]),
                    "form_channel_review_queue": int(db.execute("SELECT COUNT(*) FROM campaign_items WHERE item_status='CONTACT_FORM_REVIEW'").fetchone()[0]),
                    "simulated_delivery_events": int(db.execute("SELECT COUNT(*) FROM campaign_audit WHERE event_type='SIMULATED_DELIVERY_RESULT' AND details_json LIKE '%DELIVERED%' AND details_json NOT LIKE '%FAILED%' ").fetchone()[0]),
                    "simulated_failure_events": int(db.execute("SELECT COUNT(*) FROM campaign_audit WHERE event_type='SIMULATED_DELIVERY_RESULT' AND details_json LIKE '%FAILED%' ").fetchone()[0]),
                    "delivery_status": statuses, "responses": responses, "interest_submissions": interests,
                    "simulated_submissions": int(db.execute("SELECT COUNT(*) FROM demo_interest_submissions WHERE is_simulated=1").fetchone()[0]),
                    "simulated_followups": int(db.execute("SELECT COUNT(*) FROM demo_interest_submissions WHERE handoff_status<>'NOT_STARTED'").fetchone()[0]),
                    "real_sends": 0, "opens_or_clicks": "NOT_TRACKED", "formal_consents": 0,
                    "global_emergency_stopped": bool(db.execute("SELECT emergency_stopped FROM demo_control WHERE id=1").fetchone()[0])}

    def seed_demo_campaign(self, data: dict[str, Any], *, count: int = 20,
                           name: str = "Research Outreach Campaign") -> dict[str, Any]:
        """Create a deterministic cohort selection; synthetic behavior is seeded separately."""
        candidates = []
        for company_id, detail in data["details"].items():
            channels = [c for c in detail.get("contact_channels", []) if c.get("is_suitable_first_party")]
            if detail.get("eligibility") != "ELIGIBLE" or detail.get("identity_verification_status") != "VERIFIED" or not channels:
                continue
            candidates.append(int(company_id))
        candidates.sort()
        form_only = [cid for cid in candidates if not any(
            channel["is_suitable_first_party"] and channel.get("channel_type") != "CONTACT_FORM"
            for channel in data["details"][cid].get("contact_channels", [])
        )]
        selected = form_only[:1] + [cid for cid in candidates if cid not in form_only]
        return self.create(data, name, selected[:count])

    def reset_demo_campaign(self, campaign_id: str) -> None:
        """Explicitly remove one demo campaign and only its sidecar rows."""
        with self.connect() as db:
            row = db.execute("SELECT name,demo_only FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                return
            if not row["demo_only"]:
                raise ValueError("Refusing to reset a campaign that is not marked as test-only")
            db.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))

    def rename_campaign(self, campaign_id: str, name: str) -> dict[str, Any]:
        """Explicitly rename one campaign without changing its items or form records."""
        label = " ".join(str(name or "").split())[:120]
        if not label:
            raise ValueError("Campaign name is required")
        with self.connect() as db:
            row = db.execute("SELECT name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                raise KeyError(campaign_id)
            previous_name = row["name"]
            now = _now()
            db.execute("UPDATE campaigns SET name=?,updated_at=? WHERE id=?", (label, now, campaign_id))
            self._audit(db, campaign_id, "CAMPAIGN_RENAMED", {"previous_name": previous_name, "new_name": label})
        return self.get(campaign_id)
