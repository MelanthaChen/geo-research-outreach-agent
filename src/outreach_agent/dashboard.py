"""Local read-only professor dashboard server. Only binds loopback."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from outreach_agent.dashboard_data import (
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
)

STATIC_ROOT = Path(__file__).with_name("dashboard")


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

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/test-email/config":
                try:
                    self._json(load_test_email_settings())
                except (OSError, ValueError) as exc:
                    self._json({"error": f"Test email configuration is invalid: {exc}"}, 503)
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
                filters = {key: value for key, value in params.items() if key in {"q", "source", "verification", "eligibility", "geo", "priority", "channel"}}
                self._csv(export_companies_csv(data, filters, campaign_store), "companies.csv")
                return
            if parsed.path.startswith("/api/campaigns") or parsed.path.startswith("/api/campaign-analytics") or parsed.path.startswith("/api/companies/") and parsed.path.endswith("/outreach-history"):
                if campaign_store is None:
                    self._json({"error": "Persistent campaign store is unavailable"}, 503)
                    return
                try:
                    if parsed.path == "/api/campaigns":
                        self._json({"campaigns": campaign_store.list()})
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
                self._json({"summary": data["summary"], "filters": data["filters"], "simulated_signup_url": data["simulated_signup_url"],
                            "participation_form_configuration": data["participation_form_configuration"]})
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
            try:
                origin = self.headers.get("Origin")
                fetch_site = self.headers.get("Sec-Fetch-Site", "same-origin")
                if (origin and (urlparse(origin).scheme != "http" or urlparse(origin).netloc != self.headers.get("Host"))) or fetch_site in {"cross-site", "same-site"}:
                    self._json({"error": "Cross-origin changes are not allowed"}, 403)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 128_000:
                    self._json({"error": "Request body must be between 1 and 128000 bytes"}, 400)
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                parsed = urlparse(self.path)
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
        raise ValueError("Professor dashboard may only bind to loopback")
    data = load_dashboard_data(db, verification, delivery)
    campaign_store = CampaignStore(DEFAULT_CAMPAIGN_DB)
    email_event_store = EmailEventStore()
    server = ThreadingHTTPServer((host, port), make_handler(data, campaign_store, email_event_store))
    print(f"GEO RESEARCH OUTREACH PLATFORM — LOCAL TEST TRANSPORT — http://127.0.0.1:{port}")
    print(f"Loaded {data['summary']['total_companies']} companies read-only; test campaign events persist in {DEFAULT_CAMPAIGN_DB.name}. Ctrl+C stops the local dashboard.")
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
