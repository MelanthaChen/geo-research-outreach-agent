#!/usr/bin/env python3
"""One-shot, read-only Google Forms response synchronization.

This command never creates credentials, changes sharing, submits forms, or
prints response contents. It accepts either a user-provided service-account
key path or already-configured Application Default Credentials (ADC), supplied
by the operator outside this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from outreach_agent.google_forms_integration import (
    RESPONSE_SPREADSHEET_ID,
    authenticated_sheets_reader,
    sync_sheet_rows,
)
from outreach_agent.research_lifecycle import REGISTRY_DB, ingest_google_form_response

SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DEFAULT_RANGE = "Form Responses 1!A:Z"
DEFAULT_STATUS_FILE = Path(os.environ.get("GEO_RESEARCH_SHEETS_STATUS_FILE", "/tmp/geo-research-outreach-agent-sheets-sync.status.json"))


def write_status(path: Path, state: str, *, rows: int = 0, counts: dict | None = None, error: str | None = None) -> None:
    """Write non-sensitive health state for operators and process supervisors."""
    payload = {"state": state, "updated_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
    if counts is not None:
        payload["counts"] = counts
    if error:
        payload["error"] = error
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_service(credentials_path: Path | None = None, *, use_adc: bool = False):
    try:
        import google.auth
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Install Google Sheets support with: uv sync --extra gmail") from exc
    if credentials_path is not None:
        if not credentials_path.is_file():
            raise RuntimeError(f"service-account credential file does not exist: {credentials_path}")
        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path), scopes=[SHEETS_READ_SCOPE]
        )
    elif use_adc:
        credentials, _ = google.auth.default(scopes=[SHEETS_READ_SCOPE])
    else:
        raise RuntimeError("Choose either a service-account key path or --adc")
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def materialize_secret_credentials() -> Path | None:
    """Materialize a base64-encoded Render secret only in the ephemeral filesystem."""
    encoded = os.environ.get("GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64")
    if not encoded:
        return None
    import base64

    descriptor, filename = tempfile.mkstemp(prefix="geo-sheets-", suffix=".json")
    os.close(descriptor)
    target = Path(filename)
    target.chmod(0o600)
    target.write_bytes(base64.b64decode(encoded))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Read Google Forms responses into the local Outreach Agent registry")
    parser.add_argument("--credentials", default=os.environ.get("GEO_RESEARCH_SERVICE_ACCOUNT_FILE"), help="Existing service-account JSON path; never created by this command")
    parser.add_argument("--adc", action="store_true", help="Use already-configured Application Default Credentials (for example, impersonated service-account ADC)")
    parser.add_argument("--spreadsheet-id", default=RESPONSE_SPREADSHEET_ID)
    parser.add_argument("--range", default=os.environ.get("GEO_RESEARCH_SHEETS_RANGE", DEFAULT_RANGE))
    parser.add_argument("--registry", default=REGISTRY_DB)
    parser.add_argument("--dry-run", action="store_true", help="Read and validate rows but do not write lifecycle records")
    parser.add_argument("--watch", action="store_true", help="Poll the sheet continuously; each poll remains idempotent")
    parser.add_argument("--interval-seconds", type=float, default=300, help="Polling interval used with --watch")
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE, help="Non-sensitive JSON health file for --watch operators")
    args = parser.parse_args()
    if args.credentials and args.adc:
        raise SystemExit("Choose only one authentication mode: --credentials or --adc.")
    secret_credentials = materialize_secret_credentials()
    credentials_path = Path(args.credentials).expanduser() if args.credentials else secret_credentials
    if not credentials_path and not args.adc:
        raise SystemExit("No credentials supplied. Set GEO_RESEARCH_SERVICE_ACCOUNT_FILE, GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64, pass --credentials, or use --adc.")
    service = build_service(credentials_path, use_adc=args.adc)
    if args.interval_seconds <= 0:
        raise SystemExit("--interval-seconds must be positive")

    def stop(signum, _frame):
        write_status(args.status_file, "STOPPED")
        print(f"sync_stopped signal={signum}", flush=True)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        write_status(args.status_file, "RUNNING")
        try:
            rows = authenticated_sheets_reader(spreadsheet_id=args.spreadsheet_id, range_name=args.range, service=service)
            counts = {"read": len(rows)}
            if args.dry_run:
                print(f"read_ok rows={len(rows)} dry_run=true", flush=True)
            else:
                counts = sync_sheet_rows(rows, process=lambda row: ingest_google_form_response(row.raw, path=args.registry))
                print("sync_ok " + " ".join(f"{key}={value}" for key, value in counts.items()), flush=True)
            write_status(args.status_file, "HEALTHY", rows=len(rows), counts=counts)
        except Exception as exc:
            write_status(args.status_file, "FAILED", error=f"{type(exc).__name__}: {exc}")
            raise
        if not args.watch:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f"sync_failed: {type(exc).__name__}: {exc}") from exc
