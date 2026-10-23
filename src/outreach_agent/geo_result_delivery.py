"""Authenticated GEO result retrieval and preview-only Outreach persistence."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from outreach_agent.research_lifecycle import _connect, _hash, init_registry, now
from outreach_agent.reporting import generate_docx_report
from outreach_agent.integration import validate_integration_record


def import_geo_result(
    result: dict[str, Any],
    *,
    path: Path,
    report_directory: Path,
    template: Path,
) -> dict[str, Any]:
    """Import one GEO Engine result after validating the existing handoff.

    This is the service boundary for GEO Engine -> Outreach.  It reuses the
    existing report table and DOCX renderer; the result envelope is the stable
    idempotency key and no email is sent.
    """
    required = ("source_profile_id", "source_profile_version", "property_id", "record_id")
    missing = [key for key in required if result.get(key) in (None, "")]
    if missing:
        raise ValueError(f"research result missing: {', '.join(missing)}")
    if result.get("contract_version") != "geo-research-integration/v1":
        raise ValueError("unsupported GEO research result contract")
    if result.get("record_type") not in (None, "research_result_export"):
        raise ValueError("unexpected GEO research result record type")
    validate_integration_record({**result, "record_type": "research_result_export"})
    init_registry(path)
    with _connect(path) as db:
        record = db.execute(
            "SELECT profile_id,profile_version,profile_type,consent_status,withdrawal_status,geo_property_id FROM research_records WHERE profile_id=?",
            (result["source_profile_id"],),
        ).fetchone()
        if not record:
            raise ValueError("result profile is not registered in Outreach")
        if record["profile_version"] != result["source_profile_version"]:
            raise ValueError("result profile version does not match Outreach record")
        if record["withdrawal_status"] == "WITHDRAWN" or record["consent_status"] in {"WITHDRAWN", "DECLINED"}:
            raise PermissionError("withdrawn or declined research cannot receive a result")
        handoff = db.execute(
            "SELECT property_id,status FROM geo_handoffs WHERE profile_id=? AND profile_version=?",
            (result["source_profile_id"], result["source_profile_version"]),
        ).fetchone()
        if not handoff or handoff["status"] != "HANDED_OFF":
            raise PermissionError("result import requires a completed GEO handoff")
        if int(handoff["property_id"] or 0) != int(result["property_id"]):
            raise ValueError("GEO property does not match Outreach handoff")
        if record["geo_property_id"] not in (None, result["property_id"]):
            raise ValueError("GEO property does not match Outreach record")
        existing = db.execute(
            "SELECT report_id,report_path,status,delivery_status FROM research_reports WHERE result_record_id=?",
            (result["record_id"],),
        ).fetchone()
        if existing:
            return {"status": "EXISTING", "report_id": existing["report_id"], "report_path": existing["report_path"], "delivery_status": existing["delivery_status"]}
        conflicting = db.execute(
            "SELECT report_id FROM research_reports WHERE profile_id=? AND source_profile_version=?",
            (result["source_profile_id"], result["source_profile_version"]),
        ).fetchone()
        if conflicting:
            raise ValueError("a different result is already associated with this handoff")
    imported = persist_geo_result_preview(result, path=path, report_directory=report_directory, template=template)
    with _connect(path) as db:
        db.execute(
            "UPDATE research_records SET geo_property_id=COALESCE(geo_property_id,?), geo_research_status='REPORT_READY',updated_at=? WHERE profile_id=? AND geo_research_status NOT IN ('BLOCKED_WITHDRAWN','WITHDRAWN')",
            (result["property_id"], now(), result["source_profile_id"]),
        )
    return imported


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
