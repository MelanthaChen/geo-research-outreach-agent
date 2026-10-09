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
)
from outreach_agent.campaigns import CampaignStore, DEFAULT_CAMPAIGN_DB

STATIC_ROOT = Path(__file__).with_name("dashboard")


def make_handler(data: dict, campaign_store: CampaignStore | None = None):
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

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/campaigns") or parsed.path.startswith("/api/campaign-analytics") or parsed.path.startswith("/api/companies/") and parsed.path.endswith("/outreach-history"):
                if campaign_store is None:
                    self._json({"error": "Persistent demo campaign store is unavailable"}, 503)
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
            if parsed.path == "/api/companies":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                companies = filter_companies(data, params)
                states = campaign_store.company_states() if campaign_store else {}
                for company in companies:
                    company["outreach_status"] = states.get(company["id"], "NO_CAMPAIGN_HISTORY")
                if params.get("outreach_status"):
                    companies = [row for row in companies if row["outreach_status"] == params["outreach_status"]]
                total = len(companies)
                if "page" in params or "page_size" in params:
                    try:
                        page = max(1, int(params.get("page", "1")))
                        page_size = max(1, min(100, int(params.get("page_size", "50"))))
                    except ValueError:
                        self._json({"error": "page and page_size must be integers"}, 400)
                        return
                    companies = companies[(page - 1) * page_size:page * page_size]
                else:
                    page, page_size = 1, total
                self._json({"companies": companies, "total": total, "page": page, "page_size": page_size})
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
            if campaign_store is None:
                self._json({"error": "Demo server is read-only; campaign persistence is not enabled"}, 405)
                return
            try:
                origin = self.headers.get("Origin")
                fetch_site = self.headers.get("Sec-Fetch-Site", "same-origin")
                if (origin and (urlparse(origin).scheme != "http" or urlparse(origin).netloc != self.headers.get("Host"))) or fetch_site in {"cross-site", "same-site"}:
                    self._json({"error": "Cross-origin demo mutations are not allowed"}, 403)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 128_000:
                    self._json({"error": "Request body must be between 1 and 128000 bytes"}, 400)
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                parsed = urlparse(self.path)
                if parsed.path == "/api/campaigns":
                    company_ids = body.get("company_ids", [])
                    if body.get("all_matching") is True:
                        params = body.get("filters") or {}
                        if not isinstance(params, dict):
                            raise ValueError("filters must be a JSON object")
                        rows = filter_companies(data, {k: str(v) for k, v in params.items() if v})
                        outreach_filter = params.get("outreach_status")
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
                        self._json({"error": "Unknown simulated-submission action"}, 404)
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
    server = ThreadingHTTPServer((host, port), make_handler(data, campaign_store))
    print(f"GEO RESEARCH OUTREACH — DEMO MODE — http://127.0.0.1:{port}")
    print(f"Loaded {data['summary']['total_companies']} companies read-only; DEMO campaign events persist in {DEFAULT_CAMPAIGN_DB.name}. Ctrl+C stops the local dashboard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped. No real delivery or form submission occurred.")
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
