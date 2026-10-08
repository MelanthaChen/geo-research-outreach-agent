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

STATIC_ROOT = Path(__file__).with_name("dashboard")


def make_handler(data: dict):
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
            if parsed.path == "/api/summary":
                self._json({"summary": data["summary"], "filters": data["filters"], "simulated_signup_url": data["simulated_signup_url"]})
                return
            if parsed.path == "/api/companies":
                params = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
                self._json({"companies": filter_companies(data, params)})
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
            self._json({"error": "Demo server is read-only; simulated events stay in browser memory"}, 405)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    return DashboardHandler


def serve(host: str = "127.0.0.1", port: int = 8765, db: Path = DEFAULT_DB,
          verification: Path = DEFAULT_VERIFICATION, delivery: Path = DEFAULT_DELIVERY) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Professor dashboard may only bind to loopback")
    data = load_dashboard_data(db, verification, delivery)
    server = ThreadingHTTPServer((host, port), make_handler(data))
    print(f"GEO RESEARCH OUTREACH — DEMO MODE — http://127.0.0.1:{port}")
    print(f"Loaded {data['summary']['total_companies']} companies read-only. Ctrl+C stops the local dashboard.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped; no simulated event was persisted.")
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
