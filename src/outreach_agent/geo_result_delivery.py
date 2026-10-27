"""Authenticated GEO result retrieval and preview-only Outreach persistence."""

from __future__ import annotations

import json
import base64
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
            "SELECT profile_id,profile_version,profile_type,official_website,consent_status,withdrawal_status,geo_property_id FROM research_records WHERE profile_id=?",
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
        from outreach_agent.research_lifecycle import _same_first_party_site
        pages = result.get("observed_pages") or []
        if any(not _same_first_party_site(record["official_website"], page.get("url", "")) for page in pages):
            raise ValueError("research result evidence belongs to another website")
        existing = db.execute(
            "SELECT report_id,report_path,status,delivery_status FROM research_reports WHERE result_record_id=?",
            (result["record_id"],),
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE research_records SET geo_property_id=COALESCE(geo_property_id,?), geo_research_status=CASE WHEN geo_research_status NOT IN ('BLOCKED_WITHDRAWN','WITHDRAWN') THEN CASE WHEN geo_research_status='REPORT_SENT' THEN 'REPORT_SENT' ELSE ? END ELSE geo_research_status END, updated_at=? WHERE profile_id=?",
                (result["property_id"], "REPORT_READY" if existing["status"] == "READY" else "RESEARCH_INCOMPLETE", now(), result["source_profile_id"]),
            )
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
            "UPDATE research_records SET geo_property_id=COALESCE(geo_property_id,?), geo_research_status=?,updated_at=? WHERE profile_id=? AND geo_research_status NOT IN ('BLOCKED_WITHDRAWN','WITHDRAWN')",
            (result["property_id"], "REPORT_READY" if imported["report_status"] == "READY" else "RESEARCH_INCOMPLETE", now(), result["source_profile_id"]),
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
        baseline = result.get("baseline") or {}
        complete = bool(result.get("observed_pages") and result.get("recommendations") and baseline.get("results") and baseline.get("provider") not in {None, "CONTROLLED_FIXTURE", "MOCK", "mock"} and baseline.get("model_calls", 0) > 0)
        timestamp = now()
        db.execute(
            "INSERT INTO research_reports(report_id,profile_id,source_profile_version,property_id,result_record_id,report_path,status,delivery_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (report_id, result["source_profile_id"], result["source_profile_version"], result["property_id"], result["record_id"], str(output), "PREVIEW_ONLY", "NOT_SENT", timestamp, timestamp),
        )
        db.execute("UPDATE research_reports SET result_json=?,document_base64=?,status=?,delivery_error=? WHERE report_id=?", (json.dumps(result, sort_keys=True), base64.b64encode(output.read_bytes()).decode("ascii"), "READY" if complete else "INCOMPLETE", None if complete else "GEO Engine result lacks observed pages, grounded recommendations or a real model baseline; controlled-fixture output cannot be delivered as completed research", report_id))
        return {"status": "CREATED", "report_status": "READY" if complete else "INCOMPLETE", "report_id": report_id, "report_path": str(output), "delivery_status": "NOT_SENT"}


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


def deliver_geo_report(report_id: str, *, path, provider=None, sender=None, config=None):
    """Send a complete persisted GEO document to the invitation's original recipient."""
    from outreach_agent.email_dispatch import configured_dispatch, dispatch_approved_drafts, DispatchConfig
    from outreach_agent.geo_engine_handoff import owner_fixture_matches
    init_registry(path)
    with _connect(path) as db:
        report = db.execute("SELECT * FROM research_reports WHERE report_id=?", (report_id,)).fetchone()
        if not report:
            raise ValueError("report not found")
        record = db.execute("SELECT * FROM research_records WHERE profile_id=?", (report["profile_id"],)).fetchone()
        submission = db.execute("SELECT s.* FROM questionnaire_submissions s JOIN geo_handoffs h ON h.consent_submission_id=s.submission_id WHERE h.profile_id=? AND h.profile_version=?", (report["profile_id"], report["source_profile_version"])).fetchone()
        if not submission or record["consent_status"] != "CONSENTED" or record["withdrawal_status"] == "WITHDRAWN":
            raise PermissionError("active consent and its original invitation are required")
        recipient = submission["invitation_recipient_email"]
        if not owner_fixture_matches(record, recipient):
            raise PermissionError("report recipient does not match the authorized owner fixture")
        if report["delivery_status"] == "SENT":
            return {"status": "EXISTING", "report_id": report_id, "recipient": recipient}
        if report["status"] != "READY" or not report["document_base64"]:
            raise ValueError("GEO result is incomplete or its durable document is missing")
        draft_id = "draft-report-" + _hash(report_id)[:20]
        subject = f"GEO research report - {record['name']}"
        body = f"Hello,\n\nYour GEO research report for {record['official_website']} is attached. Please see its evidence, methodology and limitations.\n\nInvitation reference: {submission['invitation_reference']}\n\nGEO Research Outreach"
        db.execute("INSERT INTO email_drafts(draft_id,profile_id,draft_version,template_version,recipient,preview_hash,body,approval_status,approved_by,approved_at,subject,attachment_report_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(draft_id) DO NOTHING", (draft_id, record["profile_id"], "report-v1", "report-v1", recipient, _hash(body), body, "APPROVED", "AUTHORIZED_OWNER_CONSENT", now(), subject, report_id))
        db.execute("UPDATE research_reports SET delivery_draft_id=?,delivery_recipient=? WHERE report_id=?", (draft_id, recipient, report_id))
    try:
        if provider is None:
            provider, sender, config = configured_dispatch()
        outcome = dispatch_approved_drafts([draft_id], provider=provider, sender=sender, path=path, config=config or DispatchConfig())[0]
        sent = outcome.status in {"SENT", "DUPLICATE"} and bool(outcome.message_id)
        with _connect(path) as db:
            event = db.execute("SELECT created_at FROM email_events WHERE draft_id=? AND event_type='DISPATCH_ACCEPTED'", (draft_id,)).fetchone()
            db.execute("UPDATE research_reports SET delivery_status=?,sent_at=?,delivery_error=?,updated_at=? WHERE report_id=?", ("SENT" if sent else "FAILED", event["created_at"] if sent and event else None, outcome.error, now(), report_id))
            if sent:
                db.execute("UPDATE research_records SET geo_research_status='REPORT_SENT',updated_at=? WHERE profile_id=?", (now(), report["profile_id"]))
        return {"report_id": report_id, "status": outcome.status, "recipient": recipient, "message_id": outcome.message_id}
    except Exception as exc:
        with _connect(path) as db:
            db.execute("UPDATE research_reports SET delivery_status='FAILED',delivery_error=?,updated_at=? WHERE report_id=?", (str(exc), now(), report_id))
        raise


def resume_accepted_research(*, path, bridge=None, report_directory=Path("reports"), template=Path("templates/company_geo_analysis_report_template.docx"), provider=None, sender=None, config=None):
    """Resume persisted work on each scheduler tick, including after a restart."""
    from outreach_agent.geo_engine_handoff import default_bridge, process_geo_handoff, start_controlled_research, OWNER_TEST_AUTOMATION_PROFILES, owner_test_automation_enabled
    init_registry(path)
    with _connect(path) as db:
        submissions = db.execute("SELECT s.*,i.profile_id FROM questionnaire_submissions s JOIN questionnaire_invitations i ON i.invitation_id=s.invitation_id JOIN research_records r ON r.profile_id=i.profile_id WHERE s.review_status='APPROVED' AND s.research_interest='YES' AND s.research_authorization='YES' AND s.consent_status='CONSENTED' AND r.consent_status='CONSENTED' AND r.withdrawal_status!='WITHDRAWN' AND i.automation_authorized=1").fetchall()
    outcomes = []
    for submission in submissions:
        if submission["profile_id"] not in OWNER_TEST_AUTOMATION_PROFILES or not owner_test_automation_enabled(submission["profile_id"]):
            continue
        try:
            active_bridge = bridge or default_bridge()
            handoff = process_geo_handoff(submission["submission_id"], path=path, bridge=active_bridge)
            if handoff["status"] != "HANDED_OFF":
                outcomes.append(handoff); continue
            research = start_controlled_research(submission["submission_id"], path=path, bridge=active_bridge)
            if research["status"] != "COMPLETED":
                outcomes.append(research); continue
            with _connect(path) as db:
                report = db.execute("SELECT report_id FROM research_reports WHERE profile_id=?", (submission["profile_id"],)).fetchone()
            if not report:
                envelope = active_bridge.get_research_result(submission["profile_id"])
                result = envelope.get("result", envelope)
                report = import_geo_result(result, path=path, report_directory=report_directory, template=template)
            outcomes.append(deliver_geo_report(report["report_id"], path=path, provider=provider, sender=sender, config=config))
        except Exception as exc:
            outcomes.append({"profile_id": submission["profile_id"], "status": "BLOCKED", "error": str(exc)})
    return outcomes
