"""Local read-only professor dashboard server. Only binds loopback."""

from __future__ import annotations

import argparse
import base64
import csv
import hmac
import io
import json
import mimetypes
import os
import sqlite3
import tempfile
import zipfile
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from outreach_agent.dashboard_data import (
    cloud_dashboard_snapshot,
    DEFAULT_DB,
    DEFAULT_DELIVERY,
    DEFAULT_VERIFICATION,
    filter_companies,
    load_dashboard_data,
    make_draft_preview,
    export_companies_csv,
)
from outreach_agent.campaigns import CampaignStore, DEFAULT_CAMPAIGN_DB
from outreach_agent.email_test_settings import load_test_email_settings, test_email_preview, validate_test_recipient
from outreach_agent.gmail_integration import (
    EmailEventStore, GmailProviderError, GmailSetupError, authorize_gmail, send_gmail_message,
)
from outreach_agent.mail_merge import (
    DEFAULT_EXPORT_DIR, ExportLedger, assess_company_mail_merge, make_test_mail_merge, safe_file_component, save_export,
    research_mail_merge_rows, save_research_mail_merge_xlsx, save_word_mail_merge_template,
)
from outreach_agent.candidate_pool import export_candidate_csv
from outreach_agent.phase6e_screening import PHASE6E_DB, list_phase6e_candidates, phase6e_summary
from outreach_agent.phase6f import PHASE6F_DB, dashboard_overlays as phase6f_overlays, dashboard_summary as phase6f_summary
from outreach_agent.phase6g import REVIEW_DB as PHASE6G_REVIEW_DB, review_rows as phase6g_review_rows, csv_text as phase6g_csv_text, record_channel_decision as phase6g_record_channel_decision, record_company_review as phase6g_record_company_review
from outreach_agent.phase6h import PHASE6H_DB, dashboard_summary as phase6h_summary, dashboard_overlays as phase6h_overlays
from outreach_agent.phase7_smb import OUT as PHASE7_SMB_EXPORT_DIR
from outreach_agent.research_lifecycle import (
    REGISTRY_DB, approve_email_batch, create_questionnaire_invitation, get_invitation_reference, create_geo_handoff, export_records_csv,
    get_record, ingest_reply, list_records, personalized_recruitment_draft, create_post_form_confirmation_draft,
    create_manual_invitation_draft, reconcile_manual_send, generate_invitation_batch, approve_invitation_batch,
    questionnaire_url, record_simulated_send_batch, submit_questionnaire, withdraw_consent,
)
from outreach_agent.geo_engine_handoff import review_questionnaire_submission
from outreach_agent.geo_result_delivery import import_geo_result
from outreach_agent.sender_accounts import add_account, disconnect, list_accounts, set_default
from outreach_agent.combined_screening import run_combined_analysis
from outreach_agent.research_lifecycle import edit_email_draft
from outreach_agent.google_forms_integration import prefilled_form_url, sync_sheet_rows
from outreach_agent.lean_registry import review_contact
from outreach_agent.hotel_prospect_job import pause as pause_hotel_discovery, resume as resume_hotel_discovery, status as hotel_discovery_status
from outreach_agent.admin_auth import (
    CSRF_COOKIE, SESSION_COOKIE, authenticate, clear_cookie_header, cookie_header,
    csrf_valid, init_auth_store, password_hash_configured, revoke, session as auth_session,
)

STATIC_ROOT = Path(__file__).with_name("dashboard")

_PUBLIC_RESEARCH_FIELDS = (
    "profile_id", "profile_type", "name", "official_website", "industry", "location",
    "identity_verification_status", "geographic_classification", "screening_status",
    "recruitment_priority", "qualification_status",
)


def _public_research_record(row: dict) -> dict:
    """Return only non-sensitive fields suitable for the unauthenticated queue."""
    return {field: row.get(field) for field in _PUBLIC_RESEARCH_FIELDS}


def _public_summary(data: dict) -> dict:
    """Build a deliberately small public dashboard summary.

    Contact-channel, invitation, response, consent, and review counts are
    operational data and stay behind the protected dashboard API.
    """
    source = data.get("summary") or {}
    return {
        "public": True,
        "notice": "Public read-only view. Operational details require protected operator access.",
        "summary": {
            "total_companies": source.get("total_companies", 0),
            "cloud_registry": bool(source.get("cloud_registry")),
            "website_verification": {}, "eligibility": {}, "geo_opportunity": {},
            "research_priority": {}, "contact_channel_coverage": {},
            "contact_channel_classes": {}, "pending_human_reviews": {}, "outreach": {},
            "candidate_pool": {"total_candidates": source.get("total_companies", 0)},
            "phase6h": {}, "phase7_smb_target": {}, "hotel_candidate_pool": {},
            "hotel_discovery": {"status": "DISABLED"},
        },
        "filters": data.get("filters", {}),
        "simulated_signup_url": "",
        "participation_form_configuration": {},
    }


def _public_research_records(params: dict) -> dict:
    """Read the operational queue with sensitive lifecycle filters excluded."""
    filters = {
        key: params[key] for key in ("q", "profile_type", "geographic_status", "industry", "location", "sort")
        if key in params
    }
    # The public surface is intentionally limited to the operational prospect
    # view and can never be used to enumerate the raw discovery pool.
    filters["promotion_state"] = "OPERATIONAL_WITH_TEST"
    page = max(1, int(params.get("page", "1")))
    page_size = max(1, min(100, int(params.get("page_size", "50"))))
    result = list_records(REGISTRY_DB, **filters, limit=page_size, offset=(page - 1) * page_size)
    return {
        "public": True,
        "notice": "Public read-only queue. Contact, invitation, consent, and GEO operations are protected.",
        "records": [_public_research_record(row) for row in result.get("records", [])],
        "total": result.get("total", 0), "page": page, "page_size": page_size,
    }


def _public_records_by_type(params: dict, profile_type: str) -> dict:
    """Public projection used by the Companies and Hotels pages."""
    scoped = dict(params)
    scoped["profile_type"] = profile_type
    result = _public_research_records(scoped)
    result["profile_type"] = profile_type
    return result


def _public_aggregate(campaign_store: CampaignStore | None = None) -> dict:
    """Return aggregate-only operational metrics; never return row details."""
    result = {
        "public": True,
        "notice": "Aggregate read-only metrics. Personal responses and operational records are protected.",
        "companies": _public_records_by_type({"page": "1", "page_size": "1"}, "business_company")["total"],
        "hotels": _public_records_by_type({"page": "1", "page_size": "1"}, "hotel_property")["total"],
        "owner_controlled_tests": _public_records_by_type({"page": "1", "page_size": "1"}, "owner_controlled_test")["total"],
        "campaigns": [],
        "responses": {"total": 0, "by_status": {}},
    }
    if campaign_store is not None:
        analytics = campaign_store.analytics()
        result["campaigns"] = [
            {key: campaign.get(key) for key in ("id", "name", "status", "company_count", "simulated_deliveries", "simulated_response_count")}
            for campaign in campaign_store.list()
        ]
        result["responses"] = {
            "total": sum(int(value or 0) for value in (analytics.get("responses") or {}).values()),
            "by_status": {str(key): int(value or 0) for key, value in (analytics.get("responses") or {}).items()},
        }
    return result


def make_handler(data: dict, campaign_store: CampaignStore | None = None,
                 email_event_store: EmailEventStore | None = None):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "GEOResearchDashboard/1.0"

        def _json(self, value: object, status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _csv(self, payload: str, filename: str) -> None:
            body = payload.encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _review_auth_ok(self) -> bool:
            """Authorize review actions for the admin session or legacy trusted token."""
            if self._browser_session():
                return True
            expected = os.environ.get("OUTREACH_REVIEW_TOKEN", "")
            if not expected and self.server.server_address[0] in {"127.0.0.1", "localhost", "::1"}:
                return True
            supplied = self.headers.get("Authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:].strip()
            supplied = supplied or self.headers.get("X-Outreach-Review-Token", "")
            return bool(expected) and hmac.compare_digest(supplied, expected)

        def _dashboard_auth_ok(self) -> bool:
            loopback = self.server.server_address[0] in {"127.0.0.1", "localhost", "::1"}
            token = os.environ.get("OUTREACH_DASHBOARD_TOKEN") or os.environ.get("OUTREACH_REVIEW_TOKEN", "")
            user = os.environ.get("OUTREACH_BASIC_AUTH_USER", "")
            password = os.environ.get("OUTREACH_BASIC_AUTH_PASSWORD", "")
            # Keep the historical loopback developer convenience only when no
            # hosted authentication configuration exists.  A configured
            # Argon2id admin hash disables this escape hatch even on localhost.
            if loopback and os.environ.get("OUTREACH_ENV", "local").casefold() == "local" and not token and not (user and password) and not password_hash_configured():
                return True
            supplied = self.headers.get("Authorization", "")
            if supplied.lower().startswith("bearer ") and token:
                return hmac.compare_digest(supplied[7:].strip(), token)
            if supplied.lower().startswith("basic ") and user and password:
                try:
                    decoded = base64.b64decode(supplied[6:].strip()).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    return False
                return hmac.compare_digest(decoded, f"{user}:{password}")
            return False

        def _cookie(self, name: str) -> str:
            values = {}
            for item in self.headers.get("Cookie", "").split(";"):
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    values[key] = value
            return values.get(name, "")

        def _browser_session(self):
            return auth_session(REGISTRY_DB, self._cookie(SESSION_COOKIE))

        def _dashboard_authenticated(self) -> bool:
            return bool(self._browser_session() or self._dashboard_auth_ok())

        def _secure_cookie(self) -> bool:
            if os.environ.get("OUTREACH_COOKIE_SECURE", "").casefold() in {"0", "false", "no"}:
                return False
            return self.headers.get("X-Forwarded-Proto", "https").casefold() == "https"

        def _send_auth_cookie(self, name: str, value: str, *, max_age: int | None = None) -> None:
            self.send_header("Set-Cookie", cookie_header(name, value, secure=self._secure_cookie(), max_age=max_age))

        def _geo_service_auth_ok(self) -> bool:
            expected = os.environ.get("GEO_ENGINE_HANDOFF_TOKEN", "")
            supplied = self.headers.get("Authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:].strip()
            supplied = supplied or self.headers.get("X-GEO-Engine-Handoff-Token", "")
            # Compare encoded bytes so malformed/non-ASCII header values are
            # rejected as an ordinary authentication failure instead of
            # raising TypeError and terminating the request with a 502.
            return bool(expected) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/auth/session":
                current = self._browser_session()
                self._json({"authenticated": bool(current), "email": current["email"] if current else None,
                            "expires_at": current["expires_at"] if current else None,
                            "admin_configured": password_hash_configured()})
                return
            if parsed.path == "/health":
                self._json({"status": "healthy", "service": "outreach-dashboard"})
                return
            if parsed.path.startswith("/api/") and not self._dashboard_authenticated():
                self._json({"error": "dashboard authentication required"}, 401)
                return
            if parsed.path == "/api/public/summary":
                self._json({"error": "dashboard authentication required"}, 401)
                return
            if parsed.path == "/api/public/research/records":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                try:
                    self._json(_public_research_records(params))
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path in {"/api/public/companies", "/api/public/hotels"}:
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                profile_type = "hotel_property" if parsed.path.endswith("/hotels") else "business_company"
                try:
                    self._json(_public_records_by_type(params, profile_type))
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path in {"/api/public/campaigns", "/api/public/responses", "/api/public/analytics"}:
                aggregate = _public_aggregate(campaign_store)
                if parsed.path.endswith("/campaigns"):
                    self._json({"public": True, "notice": aggregate["notice"], "campaigns": aggregate["campaigns"]})
                elif parsed.path.endswith("/responses"):
                    self._json({"public": True, "notice": aggregate["notice"], "responses": aggregate["responses"]})
                else:
                    self._json(aggregate)
                return
            if parsed.path == "/api/test-email/config":
                try:
                    self._json(load_test_email_settings())
                except (OSError, ValueError) as exc:
                    self._json({"error": f"Test email configuration is invalid: {exc}"}, 503)
                return
            if parsed.path == "/api/sender-accounts":
                # Sender-account settings are currently SQLite-backed and are
                # intentionally not part of the hosted Neon registry.  Keep
                # the read-only dashboard bootstrap path healthy in cloud
                # mode instead of surfacing a misleading 502.
                if os.environ.get("OUTREACH_DATABASE_URL"):
                    self._json({"accounts": [], "production_oauth": False, "status": "CONFIGURATION_ONLY", "storage": "cloud_registry_not_configured"})
                else:
                    self._json({"accounts": list_accounts(REGISTRY_DB), "production_oauth": False, "status": "CONFIGURATION_ONLY"})
                return
            if parsed.path == "/api/test-email/events":
                events = email_event_store.recent() if email_event_store else []
                self._json({"events": events})
                return
            if parsed.path == "/api/test-email/preview":
                params = parse_qs(parsed.query)
                try:
                    recipient = params.get("recipient", [""])[0]
                    self._json(test_email_preview(recipient))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 403)
                return
            if parsed.path == "/api/companies/export.csv":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                filters = {key: value for key, value in params.items() if key in {"q", "source", "verification", "eligibility", "geo", "priority", "channel", "geographic_status"}}
                self._csv(export_companies_csv(data, filters, campaign_store), "companies.csv")
                return
            smb_exports = {
                "/api/phase7/smb/reconciliation.csv": "phase7_smb_reconciliation.csv",
                "/api/phase7/smb/contactable.csv": "phase7_smb_contactable_companies.csv",
                "/api/phase7/smb/size-review.csv": "phase7_smb_size_review_queue.csv",
                "/api/phase7/smb/cohort-overlap.csv": "phase7_cohort_domain_overlap.csv",
            }
            if parsed.path in smb_exports:
                target = PHASE7_SMB_EXPORT_DIR / smb_exports[parsed.path]
                if not target.is_file():
                    self._json({"error": "SMB reconciliation export is not available; run scripts/phase7_smb_reconcile.py"}, 404)
                    return
                self._csv(target.read_text(encoding="utf-8-sig"), target.name)
                return
            if parsed.path == "/api/phase7/hotels/cohort-membership.csv":
                target = PHASE7_SMB_EXPORT_DIR / "phase7_hotel_cohort_membership.csv"
                if not target.is_file():
                    self._json({"error": "Hotel cohort membership export is not available; run scripts/phase7_hotels.py cohort-export"}, 404)
                    return
                self._csv(target.read_text(encoding="utf-8-sig"), target.name)
                return
            if parsed.path.startswith("/api/campaigns") or parsed.path.startswith("/api/campaign-analytics") or parsed.path.startswith("/api/companies/") and parsed.path.endswith("/outreach-history"):
                if campaign_store is None:
                    self._json({"error": "Persistent campaign store is unavailable"}, 503)
                    return
                try:
                    if parsed.path == "/api/campaigns":
                        self._json({"campaigns": campaign_store.list()})
                    elif parsed.path == "/api/campaigns/form-review-queue.csv":
                        self._csv(campaign_store.form_review_queue_csv(), "contact_form_manual_review_queue.csv")
                    elif parsed.path == "/api/campaign-analytics":
                        self._json(campaign_store.analytics())
                    elif parsed.path.endswith("/outreach-history"):
                        company_id = int(parsed.path.split("/")[-2])
                        self._json({"history": campaign_store.outreach_history(company_id)})
                    else:
                        campaign_id = parsed.path.rsplit("/", 1)[-1]
                        self._json(campaign_store.get(campaign_id))
                except (ValueError, KeyError) as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/summary":
                summary = dict(data["summary"])
                # Cloud mode is backed by the configured operational registry.
                # Do not invoke the local SQLite-only discovery summaries here:
                # they are unavailable on Render and would make authentication
                # fail during the dashboard bootstrap request.
                if not summary.get("cloud_registry"):
                    summary["phase6h"] = phase6h_summary()
                try:
                    summary["hotel_discovery"] = hotel_discovery_status(REGISTRY_DB)
                except Exception as exc:
                    summary["hotel_discovery"] = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
                self._json({"summary": summary, "filters": data["filters"], "simulated_signup_url": data["simulated_signup_url"],
                            "participation_form_configuration": data["participation_form_configuration"]})
                return
            if parsed.path == "/api/hotel-discovery/status":
                try:
                    self._json(hotel_discovery_status(REGISTRY_DB))
                except Exception as exc:
                    self._json({"error": f"hotel discovery status unavailable: {exc}"}, 503)
                return
            if parsed.path == "/api/candidates":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                try:
                    page = max(1, int(params.get("page", "1")))
                    page_size = max(1, min(100, int(params.get("page_size", "50"))))
                except ValueError:
                    self._json({"error": "page and page_size must be integers"}, 400)
                    return
                candidate_db = PHASE6H_DB if PHASE6H_DB.exists() else PHASE6E_DB
                result = list_phase6e_candidates(q=params.get("q", ""), source=params.get("source", ""),
                                         status=params.get("status", ""), screen_status=params.get("screen_status", ""), page=page, page_size=page_size,
                                         db_path=candidate_db)
                result["summary"] = phase6e_summary(candidate_db)
                result["phase6f"] = phase6f_summary()
                result["phase6h"] = phase6h_summary()
                overlays = (phase6h_overlays([row["candidate_id"] for row in result["candidates"]]) if PHASE6H_DB.exists()
                            else phase6f_overlays([row["candidate_id"] for row in result["candidates"]]))
                for row in result["candidates"]:
                    if row["candidate_id"] in overlays:
                        row["phase6f"] = overlays[row["candidate_id"]]
                        for field in ("verification_status", "geo_eligibility_status", "geo_opportunity_status", "contact_status", "verification_reason", "last_inspected_at"):
                            row[field] = overlays[row["candidate_id"]][field]
                if candidate_db.exists():
                    with sqlite3.connect(f"file:{candidate_db.resolve()}?mode=ro", uri=True) as candidate_db_connection:
                        result["filters"] = {
                            "sources": [r[0] for r in candidate_db_connection.execute("SELECT DISTINCT source_name FROM candidates ORDER BY source_name")],
                            "statuses": [r[0] for r in candidate_db_connection.execute("SELECT DISTINCT verification_status FROM candidates ORDER BY verification_status")],
                            "screen_statuses": [r[0] for r in candidate_db_connection.execute("SELECT DISTINCT screen_status FROM phase6e_screening ORDER BY screen_status")],
                        }
                else:
                    result["filters"] = {"sources": [], "statuses": [], "screen_statuses": []}
                self._json(result)
                return
            if parsed.path in {"/api/phase6g/review", "/api/phase6g/review/export.csv"}:
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                review_db = PHASE6H_DB if PHASE6H_DB.exists() else PHASE6F_DB
                try:
                    rows = phase6g_review_rows(db_path=review_db, review_path=PHASE6G_REVIEW_DB,
                                               source=params.get("source", ""), confidence=params.get("confidence", ""), status=params.get("status", ""))
                except (OSError, sqlite3.Error) as exc:
                    self._json({"error": f"Phase 6G review queue unavailable: {exc}"}, 503)
                    return
                if parsed.path.endswith(".csv"):
                    self._csv(phase6g_csv_text(rows), "phase6h_contact_review_queue.csv" if PHASE6H_DB.exists() else "phase6g_contact_review_queue.csv")
                    return
                sources = sorted({r["source_name"] for r in phase6g_review_rows(db_path=review_db, review_path=PHASE6G_REVIEW_DB)})
                try:
                    page = max(1, int(params.get("page", "1")))
                    page_size = max(1, min(100, int(params.get("page_size", "20"))))
                except ValueError:
                    self._json({"error": "page and page_size must be integers"}, 400)
                    return
                counts = {value: sum(r["channel_classification"] == value for r in rows) for value in sorted({r["channel_classification"] for r in rows})}
                self._json({"rows": rows[(page - 1) * page_size:page * page_size], "total": len(rows), "page": page, "page_size": page_size,
                            "sources": sources, "classification_counts": counts,
                            "companies": len({r["candidate_id"] for r in rows})})
                return
            if parsed.path.startswith("/api/candidates/export/"):
                kind = parsed.path.rsplit("/", 1)[-1]
                phase6e_file = Path(__file__).resolve().parents[2] / "data" / kind
                if phase6e_file.name == kind and kind.startswith("phase6e_") and phase6e_file.exists():
                    self._csv(phase6e_file.read_text(encoding="utf-8-sig"), kind)
                    return
                try:
                    self._csv(export_candidate_csv(kind), f"{kind}.csv")
                except ValueError as exc:
                    self._json({"error": str(exc)}, 404)
                return
            if parsed.path in {"/api/responses", "/api/responses/export.csv"}:
                if campaign_store is None:
                    self._json({"error": "Campaign response store is unavailable"}, 503)
                    return
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                allowed = {"q", "status", "campaign_id", "source", "date_from", "date_to", "environment", "sort"}
                filters = {key: value for key, value in params.items() if key in allowed}
                if parsed.path.endswith(".csv"):
                    self._csv(campaign_store.export_responses_csv(filters), "response-results.csv")
                    return
                try:
                    result = campaign_store.response_page(
                        filters, page=int(params.get("page", "1")), page_size=int(params.get("page_size", "25"))
                    )
                except (ValueError, TypeError) as exc:
                    self._json({"error": f"Invalid response pagination: {exc}"}, 400)
                    return
                result["campaigns"] = campaign_store.list()
                self._json(result)
                return
            if parsed.path.startswith("/api/responses/"):
                if campaign_store is None:
                    self._json({"error": "Campaign response store is unavailable"}, 503)
                    return
                record_id = parsed.path.rsplit("/", 1)[-1]
                try:
                    self._json(campaign_store.response_detail(record_id))
                except KeyError:
                    self._json({"error": "Response record not found"}, 404)
                return
            if parsed.path == "/api/companies":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                companies = filter_companies(data, params)
                states = campaign_store.company_states() if campaign_store else {}
                for company in companies:
                    company["outreach_status"] = states.get(company["id"], "NO_CAMPAIGN_HISTORY")
                if params.get("outreach_status"):
                    companies = [row for row in companies if row["outreach_status"] == params["outreach_status"]]
                total = len(companies)
                try:
                    page = max(1, int(params.get("page", "1")))
                    page_size = max(1, min(100, int(params.get("page_size", "50"))))
                except ValueError:
                    self._json({"error": "page and page_size must be integers"}, 400)
                    return
                companies = companies[(page - 1) * page_size:page * page_size]
                self._json({"companies": companies, "total": total, "page": page, "page_size": page_size})
                return
            if parsed.path in {"/api/research/records", "/api/research/records.csv"}:
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                filters = {key: params[key] for key in ("q", "profile_type", "consent_status", "identity_status", "contact_status", "screening_status", "recruitment_status", "geo_status", "geographic_status", "promotion_state", "industry", "location", "sort") if key in params}
                try:
                    if parsed.path.endswith(".csv"):
                        self._csv(export_records_csv(REGISTRY_DB, **filters), "research_registry.csv")
                    else:
                        page = max(1, int(params.get("page", "1")))
                        page_size = max(1, min(100, int(params.get("page_size", "50"))))
                        self._json(list_records(REGISTRY_DB, **filters, limit=page_size, offset=(page - 1) * page_size) | {"page": page, "page_size": page_size})
                except (ValueError, OSError, sqlite3.Error) as exc:
                    self._json({"error": str(exc)}, 400)
                return
            if parsed.path.startswith("/api/research/records/"):
                profile_id = unquote(parsed.path.rsplit("/", 1)[-1])
                try:
                    self._json(get_record(profile_id, REGISTRY_DB))
                except KeyError:
                    self._json({"error": "Research record not found"}, 404)
                return
            if parsed.path == "/questionnaire":
                token = parse_qs(parsed.query).get("token", [""])[0]
                body = f"""<!doctype html><html><head><meta charset='utf-8'><title>GEO Research Questionnaire</title></head><body><main><h1>GEO research questionnaire</h1><p>Review the consent choices carefully. Website modification authorization is separate and is not requested here.</p><form method='post' action='/api/questionnaire'><input type='hidden' name='token' value='{escape(token)}'><label>Company identity confirmed <input type='checkbox' name='company_identity_confirmed' value='1' required></label><br><label>Respondent name <input name='respondent_name' required></label><br><label>Respondent role <input name='respondent_role' required></label><br><label>Contact email <input type='email' name='contact_email' required></label><br><label>Participate in research <select name='agree_research'><option value='true'>Yes</option><option value='false'>No</option></select></label><br><label>Permit public website analysis <input type='checkbox' name='website_analysis_allowed' value='1'></label><br><label>Receive recommendations <input type='checkbox' name='recommendations_requested' value='1'></label><br><label>Allow ongoing measurement <input type='checkbox' name='ongoing_measurement_allowed' value='1'></label><br><label>Optional future contact <input type='checkbox' name='future_contact_permission' value='1'></label><br><input type='hidden' name='consent_wording_version' value='geo-research-consent/v1'><button type='submit'>Submit questionnaire</button></form></main></body></html>"""
                payload = body.encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(payload))); self.send_header("X-Content-Type-Options", "nosniff"); self.end_headers(); self.wfile.write(payload)
                return
            if parsed.path == "/api/company-ids":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                rows = filter_companies(data, params)
                states = campaign_store.company_states() if campaign_store else {}
                outreach_filter = params.get("outreach_status")
                if outreach_filter:
                    rows = [row for row in rows if states.get(row["id"], "NO_CAMPAIGN_HISTORY") == outreach_filter]
                self._json({"company_ids": [row["id"] for row in rows], "total": len(rows)})
                return
            if parsed.path.startswith("/api/companies/"):
                try:
                    company_id = int(parsed.path.rsplit("/", 1)[-1])
                    detail = data["details"][company_id]
                except (ValueError, KeyError):
                    self._json({"error": "Company not found"}, 404)
                    return
                self._json(detail)
                return
            if parsed.path.startswith("/api/draft/"):
                try:
                    company_id = int(parsed.path.rsplit("/", 1)[-1])
                    query = parse_qs(parsed.query)
                    contact_id = int(query["contact_id"][0]) if query.get("contact_id") else None
                    preview = make_draft_preview(data, company_id, contact_id)
                except (ValueError, KeyError) as exc:
                    self._json({"error": "Invalid company or channel selection", "detail": str(exc)}, 400)
                    return
                self._json(preview)
                return
            relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
            target = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
                self.send_error(404)
                return
            payload = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                content_type += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path).path
            internal_result_import = parsed_path == "/api/integration/results"
            auth_login = parsed_path == "/api/auth/login"
            auth_logout = parsed_path == "/api/auth/logout"
            if self.path.startswith("/api/") and self.path != "/api/questionnaire" and not internal_result_import and not auth_login and not self._dashboard_authenticated():
                self._json({"error": "dashboard authentication required"}, 401)
                return
            if internal_result_import and not self._geo_service_auth_ok():
                self._json({"error": "GEO Engine service authentication required"}, 401)
                return
            try:
                origin = self.headers.get("Origin")
                fetch_site = self.headers.get("Sec-Fetch-Site", "same-origin")
                if (origin and (urlparse(origin).scheme not in {"http", "https"} or urlparse(origin).netloc != self.headers.get("Host"))) or fetch_site in {"cross-site", "same-site"}:
                    self._json({"error": "Cross-origin changes are not allowed"}, 403)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 128_000:
                    self._json({"error": "Request body must be between 1 and 128000 bytes"}, 400)
                    return
                raw_body = self.rfile.read(length)
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded":
                    body = {key: values[-1] for key, values in parse_qs(raw_body.decode("utf-8"), keep_blank_values=True).items()}
                else:
                    body = json.loads(raw_body)
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                parsed = urlparse(self.path)
                if parsed.path == "/api/auth/login":
                    email = str(body.get("email") or "")
                    password = str(body.get("password") or "")
                    result = authenticate(REGISTRY_DB, email, password, self.client_address[0])
                    if not result:
                        self._json({"error": "Invalid administrator email or password"}, 401)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self._send_auth_cookie(SESSION_COOKIE, result["token"], max_age=7 * 24 * 60 * 60)
                    self._send_auth_cookie(CSRF_COOKIE, result["csrf"], max_age=7 * 24 * 60 * 60)
                    payload = json.dumps({"authenticated": True, "email": result["email"], "expires_at": result["expires_at"]}).encode()
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers(); self.wfile.write(payload)
                    return
                if parsed.path == "/api/auth/logout":
                    current = self._browser_session()
                    if current and not csrf_valid(current, self.headers.get("X-CSRF-Token")):
                        self._json({"error": "CSRF validation failed"}, 403); return
                    revoke(REGISTRY_DB, self._cookie(SESSION_COOKIE), self.client_address[0])
                    self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self._send_auth_cookie(SESSION_COOKIE, "", max_age=0); self._send_auth_cookie(CSRF_COOKIE, "", max_age=0)
                    payload = b'{"authenticated":false}'; self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
                    return
                current_browser_session = self._browser_session()
                if parsed.path.startswith("/api/") and not internal_result_import and parsed.path != "/api/questionnaire" and current_browser_session and not csrf_valid(current_browser_session, self.headers.get("X-CSRF-Token")):
                    self._json({"error": "CSRF validation failed"}, 403)
                    return
                if parsed.path == "/api/questionnaire":
                    token = str(body.get("token") or "")
                    response = dict(body); response.pop("token", None)
                    response["agree_research"] = str(response.get("agree_research", "")).lower() == "true"
                    secret = os.environ.get("RESEARCH_QUESTIONNAIRE_SECRET")
                    if not secret:
                        self._json({"error": "Questionnaire ingestion is disabled until RESEARCH_QUESTIONNAIRE_SECRET is configured"}, 503)
                        return
                    result = submit_questionnaire(token, secret=secret, response=response, path=REGISTRY_DB)
                    self._json({"submission_id": result, "status": "RECEIVED_FOR_REVIEW"}, 202)
                    return
                if parsed.path == "/api/integration/results":
                    try:
                        result = import_geo_result(
                            body,
                            path=REGISTRY_DB,
                            report_directory=Path(os.environ.get("OUTREACH_REPORT_DIRECTORY", "reports")),
                            template=Path(os.environ.get("OUTREACH_REPORT_TEMPLATE", "templates/company_geo_analysis_report_template.docx")),
                        )
                    except PermissionError as exc:
                        self._json({"error": str(exc)}, 403)
                        return
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        self._json({"error": str(exc)}, 409)
                        return
                    self._json(result, 200 if result.get("status") == "EXISTING" else 201)
                    return
                if parsed.path == "/api/recruitment/draft":
                    draft_id = personalized_recruitment_draft(str(body["profile_id"]), recipient=str(body["recipient"]), questionnaire_url=str(body["questionnaire_url"]), path=REGISTRY_DB)
                    self._json({"draft_id": draft_id, "status": "PENDING_APPROVAL"}, 201)
                    return
                if parsed.path == "/api/recruitment/manual-draft":
                    secret = os.environ.get("RESEARCH_QUESTIONNAIRE_SECRET")
                    if not secret:
                        self._json({"error": "Manual invitation drafting requires RESEARCH_QUESTIONNAIRE_SECRET"}, 503)
                        return
                    result = create_manual_invitation_draft(
                        str(body["profile_id"]), delivery_recipient=str(body["delivery_recipient"]),
                        secret=secret, path=REGISTRY_DB, test_override=bool(body.get("test_override", False)),
                        expires_at=body.get("expires_at"),
                    )
                    self._json(result, 200 if result.get("status") == "EXISTING" else 201)
                    return
                if parsed.path == "/api/recruitment/batch-generate":
                    secret = os.environ.get("RESEARCH_QUESTIONNAIRE_SECRET")
                    if not secret:
                        self._json({"error": "Batch invitation drafting requires RESEARCH_QUESTIONNAIRE_SECRET"}, 503)
                        return
                    try:
                        result = generate_invitation_batch([str(value) for value in (body.get("profile_ids") or [])], secret=secret, path=REGISTRY_DB)
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        self._json({"error": str(exc)}, 400)
                        return
                    self._json(result, 200)
                    return
                if parsed.path == "/api/recruitment/selection":
                    allowed = {"q", "profile_type", "geographic_status", "industry", "location", "sort", "promotion_state"}
                    filters = {key: str(value) for key, value in (body.get("filters") or {}).items() if key in allowed and str(value)}
                    filters.setdefault("promotion_state", "OPERATIONAL_WITH_TEST")
                    try:
                        result = list_records(REGISTRY_DB, **filters, limit=500, offset=0)
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        self._json({"error": str(exc)}, 400)
                        return
                    if result["total"] > 500:
                        self._json({"error": "Narrow the filters before selecting more than 500 records", "total": result["total"], "max_selectable": 500}, 409)
                        return
                    self._json({"total": result["total"], "profile_ids": [row["profile_id"] for row in result["records"]], "scope": filters})
                    return
                if parsed.path == "/api/recruitment/batch-approve":
                    try:
                        result = approve_invitation_batch(str(body.get("batch_id") or ""), str(body.get("reviewer") or ""), path=REGISTRY_DB)
                    except (KeyError, ValueError, OSError, sqlite3.Error) as exc:
                        self._json({"error": str(exc)}, 400)
                        return
                    self._json(result, 200)
                    return
                if parsed.path == "/api/research/mail-merge/package":
                    profile_ids = [str(value) for value in (body.get("profile_ids") or []) if str(value).strip()]
                    requested_recipients = body.get("allowed_recipients")
                    allowed_recipients = None
                    if isinstance(requested_recipients, list):
                        allowed_recipients = {str(value).strip() for value in requested_recipients if str(value).strip()}
                    with tempfile.TemporaryDirectory(prefix="geo-research-mail-merge-") as directory:
                        export_dir = Path(directory)
                        rows = research_mail_merge_rows(
                            REGISTRY_DB,
                            profile_ids=profile_ids or None,
                            allowed_recipients=allowed_recipients,
                        )
                        if not rows:
                            self._json({"error": "No approved invitation drafts matched the selected records"}, 409)
                            return
                        xlsx_path, xlsx_digest = save_research_mail_merge_xlsx(export_dir, "research_queue.xlsx", rows)
                        docx_path, docx_digest = save_word_mail_merge_template(export_dir)
                        csv_path = export_dir / "research_queue.csv"
                        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()), extrasaction="ignore")
                            writer.writeheader()
                            writer.writerows(rows)
                        archive = io.BytesIO()
                        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                            bundle.write(xlsx_path, arcname=xlsx_path.name)
                            bundle.write(docx_path, arcname=docx_path.name)
                            bundle.write(csv_path, arcname=csv_path.name)
                        payload = archive.getvalue()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Disposition", 'attachment; filename="geo-research-mail-merge-package.zip"')
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if parsed.path == "/api/recruitment/screen-and-draft":
                    secret = os.environ.get("RESEARCH_QUESTIONNAIRE_SECRET")
                    if not secret:
                        self._json({"error": "Combined screening and invitation drafting requires RESEARCH_QUESTIONNAIRE_SECRET"}, 503)
                        return
                    result = run_combined_analysis(str(body["profile_id"]), mode=str(body.get("mode") or "RULE_BASED"), recipient=str(body["recipient"]), secret=secret, path=REGISTRY_DB, base_url=os.environ.get("RESEARCH_QUESTIONNAIRE_BASE_URL"), force_refetch=bool(body.get("force_refetch", False)))
                    self._json({"analysis_id": result["analysis_id"], "draft_id": result["draft_id"], "invitation_id": result["invitation_id"], "status": result["status"], "mode": result["mode"]}, 201)
                    return
                if parsed.path.startswith("/api/recruitment/draft/") and parsed.path.endswith("/edit"):
                    draft_id = unquote(parsed.path.split("/")[-2])
                    edit_email_draft(draft_id, body=str(body.get("body") or ""), subject=body.get("subject"), path=REGISTRY_DB)
                    self._json({"draft_id": draft_id, "status": "PENDING_APPROVAL"})
                    return
                if parsed.path == "/api/recruitment/invite":
                    secret = os.environ.get("RESEARCH_QUESTIONNAIRE_SECRET")
                    if not secret:
                        self._json({"error": "Questionnaire invitations are disabled until RESEARCH_QUESTIONNAIRE_SECRET is configured"}, 503)
                        return
                    invitation_id, token = create_questionnaire_invitation(str(body["profile_id"]), secret=secret, path=REGISTRY_DB, expires_at=body.get("expires_at"))
                    reference = get_invitation_reference(invitation_id, path=REGISTRY_DB)
                    self._json({"invitation_id": invitation_id, "invitation_reference": reference, "questionnaire_url": prefilled_form_url(reference), "status": "INVITED"}, 201)
                    return
                if parsed.path == "/api/recruitment/approve-batch":
                    self._json(approve_email_batch(list(body.get("draft_ids") or []), str(body.get("reviewer") or ""), path=REGISTRY_DB))
                    return
                if parsed.path == "/api/recruitment/send-simulated":
                    self._json(record_simulated_send_batch(list(body.get("draft_ids") or []), path=REGISTRY_DB))
                    return
                if parsed.path == "/api/recruitment/manual-send-reconcile":
                    result = reconcile_manual_send(
                        str(body["draft_id"]), provider=str(body.get("provider") or "OUTLOOK_WORD_MAIL_MERGE"),
                        provider_message_id=str(body.get("provider_message_id") or ""),
                        evidence_ref=str(body.get("evidence_ref") or ""), sent_at=body.get("sent_at"), path=REGISTRY_DB,
                    )
                    self._json(result, 200 if result.get("status") == "EXISTING" else 201)
                    return
                if parsed.path == "/api/recruitment/replies":
                    reply_id = ingest_reply(profile_id=str(body["profile_id"]), thread_id=str(body["thread_id"]), message_id=str(body["message_id"]), sender=str(body["sender"]), text=str(body.get("text") or ""), verified_sender=bool(body.get("verified_sender")), path=REGISTRY_DB)
                    self._json({"reply_id": reply_id, "status": "RECORDED_FOR_REVIEW"}, 202)
                    return
                if parsed.path == "/api/recruitment/post-form-confirmation":
                    result = create_post_form_confirmation_draft(str(body["submission_id"]), path=REGISTRY_DB)
                    self._json(result, 201 if result.get("status") == "DRAFT" else 200)
                    return
                if parsed.path == "/api/research/withdraw":
                    withdraw_consent(str(body["profile_id"]), reason=str(body.get("reason") or "withdrawn by participant"), path=REGISTRY_DB)
                    self._json({"status": "WITHDRAWN"})
                    return
                if parsed.path == "/api/research/contact-review":
                    if not self._review_auth_ok():
                        self._json({"error": "Contact review requires an authenticated operator session or trusted review token"}, 401)
                        return
                    result = review_contact(
                        prospect_id=str(body["prospect_id"]), contact_id=str(body["contact_id"]),
                        decision=str(body.get("decision") or ""), reviewer=str(body.get("reviewer") or ""),
                        path=REGISTRY_DB,
                    )
                    self._json(result)
                    return
                if parsed.path == "/api/research/consent-review":
                    if not self._review_auth_ok():
                        self._json({"error": "Consent review requires an authenticated operator session or trusted review token"}, 401)
                        return
                    result = review_questionnaire_submission(
                        str(body["submission_id"]), reviewer=str(body.get("reviewer") or ""),
                        company_identity_verified=bool(body.get("company_identity_verified")),
                        representative_authorized=bool(body.get("representative_authorized")),
                        consent_version_verified=bool(body.get("consent_version_verified")),
                        website_scope_verified=bool(body.get("website_scope_verified")),
                        withdrawal_checked=bool(body.get("withdrawal_checked")),
                        approve=bool(body.get("approve")), notes=str(body.get("notes") or ""), path=REGISTRY_DB,
                    )
                    self._json(result, 201 if result.get("status") == "HANDED_OFF" else 200)
                    return
                if parsed.path == "/api/research/handoff":
                    handoff_id = create_geo_handoff(
                        str(body["profile_id"]),
                        str(body["submission_id"]),
                        scope=dict(body.get("scope") or {}),
                        path=REGISTRY_DB,
                    )
                    self._json({"handoff_id": handoff_id, "status": "READY"}, 201)
                    return
                if parsed.path in {"/api/hotel-discovery/pause", "/api/hotel-discovery/resume"}:
                    if not self._review_auth_ok():
                        self._json({"error": "Hotel discovery controls require an authenticated operator session or trusted review token"}, 401)
                        return
                    if parsed.path.endswith("/pause"):
                        result = pause_hotel_discovery(REGISTRY_DB, reason=str(body.get("reason") or "operator_pause"))
                    else:
                        result = resume_hotel_discovery(REGISTRY_DB)
                    self._json(result)
                    return
                if parsed.path == "/api/phase6g/review/channel":
                    phase6g_record_channel_decision(str(body.get("candidate_id") or ""), str(body.get("channel_key") or ""),
                                                   str(body.get("decision") or ""), str(body.get("note") or ""),
                                                   db_path=PHASE6H_DB if PHASE6H_DB.exists() else PHASE6F_DB, review_path=PHASE6G_REVIEW_DB)
                    self._json({"saved": True, "decision": str(body["decision"]).upper(), "scope": "CONTACT_CHANNEL_ONLY_NOT_OUTREACH_AUTHORIZATION"})
                    return
                if parsed.path == "/api/phase6g/review/company":
                    phase6g_record_company_review(str(body.get("candidate_id") or ""), str(body.get("review_status") or ""), str(body.get("note") or ""),
                                                  db_path=PHASE6H_DB if PHASE6H_DB.exists() else PHASE6F_DB, review_path=PHASE6G_REVIEW_DB)
                    self._json({"saved": True, "review_status": str(body["review_status"]).upper(), "scope": "COMPANY_RESEARCH_REVIEW_ONLY_NOT_OUTREACH_AUTHORIZATION"})
                    return
                if parsed.path in {"/api/mail-merge/assessment", "/api/mail-merge/export"}:
                    if campaign_store is None:
                        self._json({"error": "Campaign records are unavailable"}, 503)
                        return
                    mode = str(body.get("mode") or "").upper()
                    campaign_id = str(body.get("campaign_id") or "TEST-ONLY")
                    if len(campaign_id) > 160:
                        self._json({"error": "Invalid campaign identifier"}, 400)
                        return
                    if mode != "TEST":
                        try:
                            campaign_store.get(campaign_id)
                        except KeyError:
                            self._json({"error": "Select an existing campaign"}, 404)
                            return
                    company_ids = body.get("company_ids") or []
                    if not isinstance(company_ids, list) or len(company_ids) > 5000:
                        self._json({"error": "company_ids must be a list of at most 5,000 selected companies"}, 400)
                        return
                    if parsed.path.endswith("assessment"):
                        result = assess_company_mail_merge(data, campaign_id, company_ids, campaign_store.path)
                        result.pop("rows", None)
                        result.pop("eligibility_rows", None)
                        self._json(result)
                        return
                    if mode == "TEST":
                        csv_text = make_test_mail_merge(campaign_id)
                        row_count = 2
                        exclusions = {}
                        filename = "phase6c_test_mail_merge.csv"
                    elif mode == "SELECTED":
                        if not company_ids:
                            self._json({"error": "Select companies on the Companies page before exporting", "included_count": 0}, 409)
                            return
                        result = assess_company_mail_merge(data, campaign_id, company_ids, campaign_store.path)
                        if not result["rows"]:
                            self._json({"error": "No selected company passed all live-preparation gates; no mail-merge CSV was created.",
                                        "included_count": 0, "selected_count": result["selected_count"],
                                        "excluded_count": result["excluded_count"],
                                        "exclusion_counts": result["exclusion_counts"]}, 409)
                            return
                        from outreach_agent.mail_merge import MAIL_MERGE_COLUMNS
                        import csv, io
                        output = io.StringIO(newline="")
                        writer = csv.DictWriter(output, fieldnames=MAIL_MERGE_COLUMNS, lineterminator="\r\n")
                        writer.writeheader()
                        for row in result["rows"]:
                            from outreach_agent.dashboard_data import _spreadsheet_safe
                            writer.writerow({key: _spreadsheet_safe(row.get(key)) for key in MAIL_MERGE_COLUMNS})
                        csv_text = output.getvalue()
                        row_count = len(result["rows"])
                        exclusions = result["exclusion_counts"]
                        filename = f"phase6c_mail_merge_{safe_file_component(campaign_id)}.csv"
                    else:
                        self._json({"error": "mode must be TEST or SELECTED"}, 400)
                        return
                    saved_path, file_id = save_export(DEFAULT_EXPORT_DIR, filename, csv_text)
                    ExportLedger().record(campaign_id=campaign_id, mode=mode, rows=row_count,
                                          exclusions=exclusions, file_id=file_id)
                    self._csv(csv_text, saved_path.name)
                    return
                if parsed.path == "/api/test-email/send":
                    try:
                        validate_test_recipient(body.get("recipient"))
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 403)
                        return
                    if body.get("confirm") is not True:
                        self._json({"error": "Explicit confirmation is required for a test send"}, 400)
                        return
                    settings = load_test_email_settings()
                    preview = test_email_preview(body["recipient"])
                    if body.get("preview_hash") != preview["preview_hash"]:
                        self._json({"error": "The message preview changed. Review the current recipient and message again.", "sent": False}, 409)
                        return
                    if not settings["send_enabled"]:
                        self._json({"error": settings["prerequisite"], "sent": False, "provider_accepted": False}, 503)
                        return
                    event_store = email_event_store or EmailEventStore()
                    try:
                        idempotency_key = event_store.reserve(body["recipient"], preview["subject"], preview["preview_hash"])
                    except ValueError as exc:
                        self._json({"error": str(exc), "sent": False}, 409)
                        return
                    try:
                        result = send_gmail_message(
                            recipient=preview["to"], sender=preview["from"],
                            subject=preview["subject"], body=preview["body"], settings=settings,
                        )
                    except GmailProviderError as exc:
                        status = "PROVIDER_REJECTED" if exc.provider_rejected else "SEND_FAILED_UNCERTAIN"
                        event_store.finish(idempotency_key, status, error=str(exc))
                        self._json({"error": str(exc), "sent": False,
                                    "provider_rejected": exc.provider_rejected,
                                    "delivery_confirmation": "NOT_CONFIRMED"}, 502)
                        return
                    except GmailSetupError as exc:
                        event_store.finish(idempotency_key, "SEND_FAILED", error=str(exc))
                        self._json({"error": str(exc), "sent": False, "provider_accepted": False}, 503)
                        return
                    event_store.finish(idempotency_key, result["status"], message_id=result["provider_message_id"])
                    self._json({"sent": True, "provider": "Gmail API", **result})
                    return
                if parsed.path == "/api/test-email/connect":
                    settings = load_test_email_settings()
                    try:
                        result = authorize_gmail(settings)
                    except GmailSetupError as exc:
                        self._json({"error": str(exc), "authenticated": False}, 503)
                        return
                    except Exception as exc:
                        self._json({"error": f"Google authorization did not complete ({type(exc).__name__}); no credential was saved.", "authenticated": False}, 502)
                        return
                    self._json(result)
                    return
                if parsed.path == "/api/sender-accounts":
                    account = add_account(str(body.get("email") or ""), display_name=str(body.get("display_name") or ""), provider=str(body.get("provider") or "GMAIL"), reply_to=str(body.get("reply_to") or ""), signature=str(body.get("signature") or ""), path=REGISTRY_DB)
                    self._json({"account": account, "status": "NOT_CONNECTED", "message": "Account saved. OAuth connection requires configured Google credentials; no sender was connected."}, 201)
                    return
                if parsed.path.startswith("/api/sender-accounts/"):
                    account_id = unquote(parsed.path.rsplit("/", 1)[-1])
                    action = str(body.get("action") or "")
                    if action == "set-default":
                        self._json({"account": set_default(account_id, path=REGISTRY_DB), "status": "DEFAULT_SELECTED"})
                    elif action == "disconnect":
                        self._json({"account": disconnect(account_id, path=REGISTRY_DB), "status": "DISCONNECTED"})
                    elif action == "connect":
                        self._json({"status": "NOT_CONNECTED", "error": "Production OAuth is not configured; no credential was requested or saved."}, 503)
                    else:
                        self._json({"error": "Unknown sender account action"}, 400)
                    return
                if campaign_store is None:
                    self._json({"error": "Campaign persistence is not enabled"}, 405)
                    return
                if parsed.path == "/api/campaigns":
                    company_ids = body.get("company_ids", [])
                    if body.get("all_matching") is True:
                        params = body.get("filters") or {}
                        if not isinstance(params, dict):
                            raise ValueError("filters must be a JSON object")
                        filtered = {k: str(v) for k, v in params.items() if v}
                        rows = filter_companies(data, filtered)
                        outreach_filter = filtered.get("outreach_status")
                        states = campaign_store.company_states()
                        if outreach_filter:
                            rows = [row for row in rows if states.get(row["id"], "NO_CAMPAIGN_HISTORY") == outreach_filter]
                        company_ids = [row["id"] for row in rows]
                    result = campaign_store.create(data, body.get("name", ""), company_ids)
                    self._json(result, 201)
                    return
                if parsed.path == "/api/demo/interest":
                    self._json(campaign_store.submit_demo_interest(body.get("form_token", ""), body))
                    return
                if parsed.path == "/api/demo/form-open":
                    self._json(campaign_store.open_demo_form(body.get("form_token", "")))
                    return
                if parsed.path == "/api/demo/emergency-stop":
                    self._json(campaign_store.global_emergency_stop(bool(body.get("enabled", True))))
                    return
                if parsed.path.startswith("/api/demo/submissions/"):
                    parts = parsed.path.strip("/").split("/")
                    if len(parts) != 5 or parts[4] not in {"review", "handoff"}:
                        self._json({"error": "Unknown test-submission action"}, 404)
                        return
                    submission_id = int(parts[3])
                    result = (campaign_store.review_demo_submission(submission_id) if parts[4] == "review"
                              else campaign_store.begin_demo_handoff(submission_id))
                    self._json(result)
                    return
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["api", "campaigns"]:
                    campaign_id = parts[2]
                    action = parts[3] if len(parts) > 3 else ""
                    if action == "approve":
                        result = campaign_store.approve_demo(campaign_id)
                    elif action == "status":
                        result = campaign_store.set_status(campaign_id, body.get("status", ""))
                    elif action == "simulate-batch":
                        outcomes = body.get("outcomes") or {}
                        if not isinstance(outcomes, dict):
                            raise ValueError("outcomes must be a company-ID mapping")
                        result = campaign_store.simulate_batch(campaign_id, body.get("limit", 10),
                                                               {int(key): value for key, value in outcomes.items()})
                    elif action == "emergency-stop":
                        result = campaign_store.emergency_stop(campaign_id)
                    elif action == "responses":
                        result = campaign_store.record_response(campaign_id, int(body["company_id"]), body.get("response", ""))
                    else:
                        self._json({"error": "Unknown campaign action"}, 404)
                        return
                    self._json(result)
                    return
                self._json({"error": "No live delivery or form-submission route exists"}, 405)
            except KeyError as exc:
                self._json({"error": f"Missing or unknown value: {exc}"}, 404)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 400)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return DashboardHandler


def serve(host: str = "127.0.0.1", port: int = 8765, db: Path = DEFAULT_DB,
          verification: Path = DEFAULT_VERIFICATION, delivery: Path = DEFAULT_DELIVERY) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        if os.environ.get("OUTREACH_ALLOW_REMOTE", "").casefold() not in {"1", "true", "yes"}:
            raise ValueError("remote dashboard binding requires OUTREACH_ALLOW_REMOTE=true")
        if not (os.environ.get("OUTREACH_DASHBOARD_TOKEN") or (os.environ.get("OUTREACH_BASIC_AUTH_USER") and os.environ.get("OUTREACH_BASIC_AUTH_PASSWORD"))):
            raise ValueError("remote dashboard binding requires dashboard token or basic-auth credentials")
    cloud_registry = os.environ.get("OUTREACH_DATABASE_URL")
    init_auth_store(REGISTRY_DB)
    data = cloud_dashboard_snapshot(cloud_registry) if cloud_registry else load_dashboard_data(db, verification, delivery)
    campaign_store = CampaignStore(DEFAULT_CAMPAIGN_DB)
    email_event_store = EmailEventStore()
    server = ThreadingHTTPServer((host, port), make_handler(data, campaign_store, email_event_store))
    print(f"GEO RESEARCH OUTREACH PLATFORM — LOCAL TEST TRANSPORT — http://127.0.0.1:{port}")
    source_label = "OUTREACH_DATABASE_URL" if cloud_registry else DEFAULT_CAMPAIGN_DB.name
    print(f"Loaded {data['summary']['total_companies']} research records read-only from {source_label}. Ctrl+C stops the local dashboard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped. No live delivery or form submission occurred.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION)
    parser.add_argument("--delivery-events", type=Path, default=DEFAULT_DELIVERY)
    args = parser.parse_args()
    serve(args.host, args.port, args.database, args.verification, args.delivery_events)


if __name__ == "__main__":
    main()
