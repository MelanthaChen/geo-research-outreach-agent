#!/usr/bin/env python3
"""One-shot, read-only Google Forms response synchronization.

This command never creates credentials, changes sharing, submits forms, or
prints response contents. It accepts either a user-provided service-account
key path or already-configured Application Default Credentials (ADC), supplied
by the operator outside this repository.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from outreach_agent.google_forms_integration import (
    RESPONSE_SPREADSHEET_ID,
    authenticated_sheets_reader,
    sync_sheet_rows,
)
from outreach_agent.research_lifecycle import REGISTRY_DB, ingest_google_form_response

SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DEFAULT_RANGE = "Form Responses 1!A:Z"


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Read Google Forms responses into the local Outreach Agent registry")
    parser.add_argument("--credentials", default=os.environ.get("GEO_RESEARCH_SERVICE_ACCOUNT_FILE"), help="Existing service-account JSON path; never created by this command")
    parser.add_argument("--adc", action="store_true", help="Use already-configured Application Default Credentials (for example, impersonated service-account ADC)")
    parser.add_argument("--spreadsheet-id", default=RESPONSE_SPREADSHEET_ID)
    parser.add_argument("--range", default=os.environ.get("GEO_RESEARCH_SHEETS_RANGE", DEFAULT_RANGE))
    parser.add_argument("--registry", type=Path, default=REGISTRY_DB)
    parser.add_argument("--dry-run", action="store_true", help="Read and validate rows but do not write lifecycle records")
    args = parser.parse_args()
    if args.credentials and args.adc:
        raise SystemExit("Choose only one authentication mode: --credentials or --adc.")
    if not args.credentials and not args.adc:
        raise SystemExit("No credentials supplied. Set GEO_RESEARCH_SERVICE_ACCOUNT_FILE or pass --credentials, or use --adc after local ADC setup.")
    service = build_service(Path(args.credentials).expanduser() if args.credentials else None, use_adc=args.adc)
    rows = authenticated_sheets_reader(spreadsheet_id=args.spreadsheet_id, range_name=args.range, service=service)
    if args.dry_run:
        print(f"read_ok rows={len(rows)} dry_run=true")
        return 0
    counts = sync_sheet_rows(rows, process=lambda row: ingest_google_form_response(row.raw, path=args.registry))
    print("sync_ok " + " ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f"sync_failed: {type(exc).__name__}: {exc}") from exc
