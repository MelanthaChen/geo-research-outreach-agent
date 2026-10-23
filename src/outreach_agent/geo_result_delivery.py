"""Authenticated GEO result retrieval and preview-only Outreach persistence."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from outreach_agent.research_lifecycle import _connect, _hash, init_registry, now
from outreach_agent.reporting import generate_docx_report


def persist_geo_result_preview(
    result: dict[str, Any],
    *,
    path: Path,
    report_directory: Path,
    template: Path,
) -> dict[str, Any]:
    """Persist one imported result and render a report preview idempotently.

    This function never sends email and never changes consent or handoff state.
    A repeated result record returns the existing preview instead of creating a
    second report.
    """
    required = ("source_profile_id", "source_profile_version", "property_id", "record_id")
    missing = [key for key in required if result.get(key) in (None, "")]
    if missing:
        raise ValueError(f"research result missing: {', '.join(missing)}")
    init_registry(path)
    report_id = "report-" + _hash(result["record_id"])[:20]
    output = report_directory / f"{report_id}.docx"
    with _connect(path) as db:
        existing = db.execute("SELECT report_id,report_path,status,delivery_status FROM research_reports WHERE result_record_id=?", (result["record_id"],)).fetchone()
        if existing:
            return {"status": "EXISTING", "report_id": existing["report_id"], "report_path": existing["report_path"], "delivery_status": existing["delivery_status"]}
        generate_docx_report(result, output, template)
        timestamp = now()
        db.execute(
            "INSERT INTO research_reports(report_id,profile_id,source_profile_version,property_id,result_record_id,report_path,status,delivery_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (report_id, result["source_profile_id"], result["source_profile_version"], result["property_id"], result["record_id"], str(output), "PREVIEW_ONLY", "NOT_SENT", timestamp, timestamp),
        )
        return {"status": "CREATED", "report_id": report_id, "report_path": str(output), "delivery_status": "NOT_SENT"}


def report_email_preview(result: dict[str, Any], *, recipient: str) -> dict[str, str]:
    """Build a final-report preview without persisting a send or sending mail."""
    if not recipient or "@" not in recipient:
        raise ValueError("recipient must be a valid email address")
    strategies = result.get("strategies") or []
    body = (
        f"Research result preview for {result.get('source_profile_id')}\n\n"
        "This is a human-review preview. It does not represent delivery or a measured improvement.\n\n"
        f"Property ID: {result.get('property_id')}\n"
        f"Top strategies: {json.dumps(strategies, ensure_ascii=False)}\n"
        f"Limitations: {json.dumps(result.get('limitations', []), ensure_ascii=False)}\n"
    )
    return {"preview_id": "report-email-" + secrets.token_urlsafe(8), "recipient": recipient, "subject": "GEO research report preview", "body": body, "status": "PREVIEW_ONLY"}
