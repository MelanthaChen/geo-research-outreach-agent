"""Safe CSV preparation for offline previews and human-reviewed mail merge.

This module creates files only. It has no email, SMTP, form submission, or
company-database write capability. Export history is kept in a separate local
ledger rather than changing campaign or cohort records.
"""

from __future__ import annotations

import csv
from copy import copy
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml

from outreach_agent.dashboard_data import DEFAULT_OUTREACH_CONFIG, ROOT, _suitable_channel, _spreadsheet_safe, export_companies_csv, make_draft_preview
from outreach_agent.outreach_pipeline import DEFAULT_RESEARCH_SUBJECT, DEMO_PARTICIPATION_FORM_URL, mail_merge_safe_subject, render_template, validate_recipient

TEST_RECIPIENTS = ("yuxaun.chen.1031@outlook.com", "yuxuan.chen.1031@outlook.com")
MAIL_MERGE_COLUMNS = (
    "company_id", "company_name", "recipient_email", "email_subject", "email_body",
    "participation_form_url", "campaign_id", "contact_channel", "contact_evidence_url",
    "eligibility_status", "review_status",
)
RESEARCH_MAIL_MERGE_COLUMNS = (
    # Word for Mac only enables Merge to E-Mail when it can identify a
    # conventional email-address column. Keep the machine-readable field and
    # add the compatibility alias; both are sourced from the same validated
    # recipient and are never operator-editable send authorization.
    "profile_id", "company_name", "recipient_email", "Email Address", "email_subject", "email_body",
    "questionnaire_url", "invitation_reference", "draft_id", "invitation_id",
    "draft_status", "invitation_status", "merge_status",
    *(f"body_paragraph_{index:02d}" for index in range(1, 33)),
)
ELIGIBILITY_COLUMNS = (
    "campaign_id", "company_id", "company_name", "identity_status", "eligibility_status", "company_review_status",
    "candidate_email_count", "selected_channel_review_status", "contact_evidence_url",
    "campaign_duplicate", "suppressed", "participation_form_ready", "invitation_token_available",
    "export_status", "exclusion_reason",
)
DEFAULT_EXPORT_DIR = ROOT / "exports"
DEFAULT_LEDGER = Path.home() / ".local" / "share" / "geo-research-outreach-agent" / "phase6c_export_tracking.sqlite3"
DEFAULT_TOKEN_FILE = Path.home() / ".local" / "share" / "geo-research-outreach-agent" / "live_invitation_tokens.json"
SUPPRESSION_FILE = ROOT / "data/outreach_suppression.csv"


def _csv_text(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _spreadsheet_safe(row.get(key)) for key in columns})
    return output.getvalue()


def _read_campaign_membership(db_path: Path, campaign_id: str) -> tuple[set[int], set[int]]:
    """Return companies already present in this campaign and company opt-outs."""
    if not campaign_id or not db_path.exists():
        return set(), set()
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        members = {int(row[0]) for row in db.execute(
            "SELECT company_id FROM campaign_items WHERE campaign_id=?", (campaign_id,)
        )}
        optouts = {int(row[0]) for row in db.execute(
            "SELECT DISTINCT company_id FROM campaign_items WHERE response_status='OPTED_OUT'"
        )}
    return members, optouts


def _suppressed_emails(path: Path = SUPPRESSION_FILE) -> set[str]:
    if not path.exists():
        return set()
    import csv as csv_module
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {str(row.get("email") or "").strip().casefold() for row in csv_module.DictReader(handle) if row.get("email")}


def _form_base(config: dict[str, Any]) -> str:
    url = str(config.get("participation_interest_form_url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.casefold().endswith(".invalid"):
        return ""
    return url


def _load_tokens(path: Path = DEFAULT_TOKEN_FILE) -> dict[int, str]:
    """Load private real-form tokens, never copied into the general company export."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {int(key): str(value) for key, value in raw.items()
            if str(key).isdigit() and isinstance(value, str) and len(value) >= 32}


def _tokenized_url(base: str, token: str) -> str:
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}invitation_token={quote(token, safe='')}"


def safe_file_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:48] or "campaign"


def make_test_mail_merge(campaign_id: str = "TEST-ONLY") -> str:
    """Create exactly two clearly synthetic formatting rows; never read cohort contacts."""
    rows = []
    for index, recipient in enumerate(TEST_RECIPIENTS, start=1):
        company_name = f"Synthetic Test Company {index}"
        link = f"DEMO PLACEHOLDER ONLY — NOT A WORKING FORM: {DEMO_PARTICIPATION_FORM_URL}"
        body = (
            f"OFFLINE TEST DRAFT — SYNTHETIC COMPANY DATA — NOT SENT\n\n"
            f"Hello {company_name} Team,\n\n"
            "We are testing the formatting of a university research invitation about Generative Engine Optimization (GEO).\n\n"
            f"Synthetic participation link (not active): {link}\n\n"
            "This test message is not a real invitation, and expressing interest is not formal research consent. "
            "For questions only, use [RESEARCH CONTACT EMAIL NOT CONFIGURED].\n\n"
            "[RESEARCHER NAME NOT CONFIGURED]\n[RESEARCH TEAM NOT CONFIGURED]\n[UNIVERSITY AFFILIATION NOT CONFIGURED]"
        )
        rows.append({
            "company_id": f"SYNTHETIC-{index:03d}", "company_name": company_name,
            "recipient_email": recipient, "email_subject": "[OFFLINE TEST] Invitation formatting preview",
            "email_body": body, "participation_form_url": link, "campaign_id": campaign_id,
            "contact_channel": "SYNTHETIC_TEST_ONLY", "contact_evidence_url": "",
            "eligibility_status": "SYNTHETIC_TEST_ONLY", "review_status": "NOT_APPLICABLE_TEST_ONLY",
        })
    return _csv_text(MAIL_MERGE_COLUMNS, rows)


def assess_company_mail_merge(
    data: dict[str, Any], campaign_id: str, company_ids: list[int], campaign_db: Path,
    *, config_path: Path = DEFAULT_OUTREACH_CONFIG, suppression_path: Path = SUPPRESSION_FILE,
    token_path: Path = DEFAULT_TOKEN_FILE,
) -> dict[str, Any]:
    """Evaluate selected companies fail-closed; returns preview rows and all exclusion counts."""
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    form_base = _form_base(config)
    tokens = _load_tokens(token_path)
    members, opted_out_ids = _read_campaign_membership(campaign_db, campaign_id)
    suppressed = _suppressed_emails(suppression_path)
    exclusions: dict[str, int] = {}
    eligibility_rows: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, str]] = set()
    seen_recipients: set[str] = set()
    for company_id in sorted(set(int(value) for value in company_ids)):
        detail = data["details"].get(company_id)
        if not detail:
            continue
        candidates = sorted(
            (channel for channel in detail.get("contact_channels", [])
             if channel.get("channel_type") != "CONTACT_FORM" and _suitable_channel(detail, channel)
             and str(channel.get("email") or "").strip()),
            key=lambda channel: (str(channel.get("review_status") or "PENDING").upper() != "APPROVED", int(channel.get("id") or 0)),
        )
        company_status = str(detail.get("review_status") or "PENDING").upper()
        identity = str(detail.get("identity_verification_status") or "UNKNOWN").upper()
        geographic = str(detail.get("geographic_classification") or "COUNTRY_UNKNOWN").upper()
        eligibility = str(detail.get("eligibility") or "NEEDS_REVIEW").upper()
        chosen = candidates[0] if candidates else None
        email = ""
        if chosen:
            try:
                email = validate_recipient(chosen.get("email"))
            except ValueError:
                chosen = None
        if chosen and geographic == "VERIFIED_US" and identity == "VERIFIED" and eligibility == "ELIGIBLE":
            try:
                preview = make_draft_preview(data, company_id, int(chosen["id"]))
            except (KeyError, ValueError, OSError):
                preview = {}
            if preview.get("status") == "DRAFT_READY":
                previews.append({
                    "company_id": company_id, "company_name": detail.get("company_name"),
                    "recipient_email": email, "email_subject": preview["subject"],
                    "email_body": "PREVIEW ONLY — NOT READY TO SEND\n\n" + preview["body"],
                    "participation_form_url": preview["participation_interest_form_url"],
                    "campaign_id": campaign_id, "contact_channel": chosen.get("channel_type"),
                    "contact_evidence_url": chosen.get("source_url") or "",
                    "eligibility_status": eligibility, "review_status": "PREVIEW_ONLY_NOT_APPROVED",
                })
        channel_review = str(chosen.get("review_status") or "PENDING").upper() if chosen else "NOT_AVAILABLE"
        reasons: list[str] = []
        # Count each blocker independently so the report shows every missing requirement.
        if identity != "VERIFIED":
            reasons.append("IDENTITY_NOT_VERIFIED")
        if geographic != "VERIFIED_US":
            reasons.append("GEOGRAPHY_NOT_VERIFIED_US")
        elif eligibility != "ELIGIBLE":
            reasons.append("ELIGIBILITY_NOT_APPROVED")
        if company_status != "APPROVED":
            reasons.append("COMPANY_HUMAN_REVIEW_INCOMPLETE")
        if not chosen:
            reasons.append("NO_SUITABLE_FIRST_PARTY_EMAIL")
        else:
            if not chosen.get("source_url"):
                reasons.append("CONTACT_EVIDENCE_MISSING")
            if channel_review != "APPROVED":
                reasons.append("CONTACT_HUMAN_REVIEW_INCOMPLETE")
            if email in suppressed:
                reasons.append("SUPPRESSED_EMAIL")
        if company_id in opted_out_ids:
            reasons.append("COMPANY_OPTED_OUT")
        if company_id in members:
            reasons.append("ALREADY_IN_SELECTED_CAMPAIGN")
        if not form_base:
            reasons.append("VALID_HTTPS_PARTICIPATION_FORM_NOT_CONFIGURED")
        if company_id not in tokens:
            reasons.append("COMPANY_INVITATION_TOKEN_UNAVAILABLE")
        if chosen and (company_id, email) in seen_pairs:
            reasons.append("DUPLICATE_COMPANY_CAMPAIGN_RECIPIENT")
        if chosen and email in seen_recipients:
            reasons.append("DUPLICATE_MAIL_MERGE_RECIPIENT")
        for reason in reasons:
            exclusions[reason] = exclusions.get(reason, 0) + 1
        if not reasons:
            seen_pairs.add((company_id, email))
            seen_recipients.add(email)
            invite_url = _tokenized_url(form_base, tokens[company_id])
            template_file = ROOT / str(config.get("template_file") or "config/templates/research_invitation.txt")
            subject = mail_merge_safe_subject(str(config.get("subject_template") or DEFAULT_RESEARCH_SUBJECT))
            context = {
                "company_name": str(detail.get("company_name") or "Business"),
                "participation_interest_form_url": invite_url,
                "research_contact_email": str(config.get("research_contact_email") or "[RESEARCH CONTACT EMAIL NOT CONFIGURED]"),
                "researcher_name": str(config.get("researcher_name") or "[RESEARCHER NAME NOT CONFIGURED]"),
                "research_team": str(config.get("research_team") or "[RESEARCH TEAM NOT CONFIGURED]"),
                "university_affiliation": str(config.get("university_affiliation") or "[UNIVERSITY AFFILIATION NOT CONFIGURED]"),
            }
            body = render_template(template_file.read_text(encoding="utf-8"), context)
            included.append({
                "company_id": company_id, "company_name": detail.get("company_name"),
                "recipient_email": email, "email_subject": subject, "email_body": body,
                "participation_form_url": invite_url, "campaign_id": campaign_id,
                "contact_channel": chosen.get("channel_type"), "contact_evidence_url": chosen.get("source_url"),
                "eligibility_status": eligibility, "review_status": "COMPANY_AND_CONTACT_APPROVED",
            })
        eligibility_rows.append({
            "campaign_id": campaign_id, "company_id": company_id, "company_name": detail.get("company_name"),
            "identity_status": identity, "eligibility_status": eligibility,
            "company_review_status": company_status, "candidate_email_count": len(candidates),
            "selected_channel_review_status": channel_review,
            "contact_evidence_url": chosen.get("source_url") if chosen else "",
            "campaign_duplicate": "YES" if company_id in members else "NO",
            "suppressed": "YES" if email and email in suppressed else "NO",
            "participation_form_ready": "YES" if form_base else "NO",
            "invitation_token_available": "YES" if company_id in tokens else "NO",
            "export_status": "PREPARED" if not reasons else "EXCLUDED",
            "exclusion_reason": " | ".join(reasons),
        })
    included.sort(key=lambda row: (str(row["company_name"]).casefold(), int(row["company_id"]), row["recipient_email"].casefold()))
    previews.sort(key=lambda row: (str(row["company_name"]).casefold(), int(row["company_id"]), row["recipient_email"].casefold()))
    eligibility_rows.sort(key=lambda row: (str(row["company_name"]).casefold(), int(row["company_id"])))
    return {
        "campaign_id": campaign_id, "selected_count": len(eligibility_rows), "included_count": len(included),
        "excluded_count": sum(1 for row in eligibility_rows if row["export_status"] == "EXCLUDED"),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "preview_rows": previews[:20], "rows": included, "eligibility_rows": eligibility_rows,
    }


def make_eligibility_report(data: dict[str, Any], campaign_id: str, campaign_db: Path, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    result = assess_company_mail_merge(data, campaign_id, sorted(data["details"]), campaign_db, **kwargs)
    return _csv_text(ELIGIBILITY_COLUMNS, result["eligibility_rows"]), result


def make_all_company_export(data: dict[str, Any], campaign_db: Path) -> str:
    """Reuse the established complete-cohort exporter with a read-only campaign snapshot."""
    def states() -> dict[int, dict[str, str]]:
        if not campaign_db.exists():
            return {}
        with sqlite3.connect(f"file:{campaign_db.resolve()}?mode=ro", uri=True) as db:
            rows = db.execute(
                """SELECT i.company_id,c.name campaign_name,
                          MAX(CASE WHEN i.delivery_status='SIMULATED_DELIVERED' THEN
                              (SELECT MAX(a.created_at) FROM campaign_audit a WHERE a.item_id=i.id AND a.event_type='SIMULATED_DELIVERY_RESULT')
                              ELSE '' END) last_delivery_at,
                          MAX(CASE WHEN i.delivery_status='SIMULATED_DELIVERED' THEN i.delivery_status ELSE '' END) delivery_status,
                          MAX(i.response_status),MAX(i.interest_status)
                   FROM campaign_items i JOIN campaigns c ON c.id=i.campaign_id
                   GROUP BY i.company_id,c.id ORDER BY i.company_id,c.name COLLATE NOCASE"""
            ).fetchall()
        grouped: dict[int, dict[str, list[str]]] = {}
        for company_id, campaign, delivered, status, response, interest in rows:
            item = grouped.setdefault(int(company_id), {key: [] for key in ("campaigns", "dates", "statuses", "responses", "interests")})
            item["campaigns"].append(str(campaign))
            if delivered:
                item["dates"].append(str(delivered))
            for key, value in (("statuses", status), ("responses", response), ("interests", interest)):
                if value:
                    item[key].append(str(value))
        return {company_id: {
            "campaign_membership": " | ".join(sorted(set(item["campaigns"]), key=str.casefold)),
            "outreach_status": " | ".join(sorted(set(item["statuses"]))) or "CAMPAIGN_DRAFTED",
            "last_outreach_timestamp": max(item["dates"], default=""),
            "response_status": " | ".join(sorted(set(item["responses"]))),
            "interest_form_submission_status": " | ".join(sorted(set(item["interests"]))),
        } for company_id, item in grouped.items()}
    return export_companies_csv(data, campaign_store=types.SimpleNamespace(company_export_states=states))


class ExportLedger:
    """Separate append-only audit for prepared/downloaded files; never marks items sent."""

    def __init__(self, path: Path = DEFAULT_LEDGER):
        self.path = Path(path)

    def record(self, *, campaign_id: str, mode: str, rows: int, exclusions: dict[str, int], file_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS exports (
                id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL, exported_at TEXT NOT NULL,
                export_mode TEXT NOT NULL, row_count INTEGER NOT NULL,
                exclusion_counts_json TEXT NOT NULL, file_id TEXT NOT NULL,
                record_status TEXT NOT NULL DEFAULT 'EXPORTED_PREPARED'
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(exports)")}
            if "record_status" not in columns:
                db.execute("ALTER TABLE exports ADD COLUMN record_status TEXT NOT NULL DEFAULT 'EXPORTED_PREPARED'")
            db.execute("INSERT INTO exports(campaign_id,exported_at,export_mode,row_count,exclusion_counts_json,file_id,record_status) VALUES(?,?,?,?,?,?,?)",
                       (campaign_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), mode, rows,
                        json.dumps(exclusions, sort_keys=True), file_id, "EXPORTED_PREPARED"))


def save_export(directory: Path, filename: str, content: str) -> tuple[Path, str]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    path = directory / filename
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=directory,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path, digest


def research_mail_merge_rows(registry_db: Path, *, profile_ids: list[str] | None = None,
                             include_pending: bool = False,
                             draft_ids: list[str] | None = None,
                             allowed_recipients: set[str] | None = None) -> list[dict[str, Any]]:
    """Build Word-compatible rows from the existing Research Queue.

    Approved rows are the only rows eligible for a sendable export. Pending
    rows may be included solely as clearly labelled review previews. This
    function is read-only with respect to the registry.
    """
    from outreach_agent.research_lifecycle import init_registry, _connect
    init_registry(registry_db)
    where = ""
    params: list[Any] = []
    if profile_ids:
        marks = ",".join("?" for _ in profile_ids)
        where = f" AND r.profile_id IN ({marks})"
        params.extend(profile_ids)
    if draft_ids is not None:
        if not draft_ids:
            return []
        where += " AND d.draft_id IN (" + ",".join("?" for _ in draft_ids) + ")"
        params.extend(draft_ids)
    statuses = ("'APPROVED','PENDING'" if include_pending else "'APPROVED'")
    rows: list[dict[str, Any]] = []
    with _connect(registry_db) as db:
        query = f"""SELECT r.name,d.draft_id,d.profile_id,d.recipient,d.subject,d.body,
                    d.approval_status,i.invitation_id,i.invitation_reference,i.status AS invitation_status
                    FROM email_drafts d JOIN research_records r ON r.profile_id=d.profile_id
                    LEFT JOIN questionnaire_invitations i ON i.invitation_id=d.invitation_id
                    WHERE d.approval_status IN ({statuses}) {where}
                    ORDER BY lower(r.name),d.draft_id"""
        for row in db.execute(query, params):
            recipient = str(row["recipient"]).casefold()
            if allowed_recipients is not None and recipient not in {x.casefold() for x in allowed_recipients}:
                continue
            merge_status = "READY_FOR_WORD_MAIL_MERGE" if row["approval_status"] == "APPROVED" else "REVIEW_ONLY_NOT_APPROVED"
            body = str(row["body"] or "")
            urls = re.findall(r"https?://[^\s<>]+", body)
            paragraphs = body.split("\n\n")
            paragraph_fields = {f"body_paragraph_{index:02d}": value for index, value in enumerate(paragraphs[:32], start=1)}
            rows.append({
                "profile_id": row["profile_id"], "company_name": row["name"],
                "recipient_email": row["recipient"], "Email Address": row["recipient"],
                "email_subject": mail_merge_safe_subject(row["subject"]),
                "email_body": body, "questionnaire_url": next((url for url in urls if "docs.google.com/forms/" in url), urls[0] if len(urls) == 1 else ""),
                "invitation_reference": row["invitation_reference"] or "",
                "draft_id": row["draft_id"], "invitation_id": row["invitation_id"] or "",
                "draft_status": row["approval_status"], "invitation_status": row["invitation_status"] or "",
                "merge_status": merge_status,
                **paragraph_fields,
            })
    return rows


def save_research_mail_merge_xlsx(directory: Path, filename: str, rows: list[dict[str, Any]]) -> tuple[Path, str]:
    """Write a local XLSX data source for Word Mailings > Merge to E-mail."""
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("XLSX export requires the optional mail_merge dependency") from exc
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ResearchQueue"
    sheet.append(list(RESEARCH_MAIL_MERGE_COLUMNS))
    for row in rows:
        sheet.append([row.get(column, "") for column in RESEARCH_MAIL_MERGE_COLUMNS])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        font = copy(cell.font)
        font.bold = True
        cell.font = font
    temporary = directory / f".{path.name}.tmp"
    workbook.save(temporary)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    os.replace(temporary, path)
    return path, digest


def save_word_mail_merge_template(directory: Path, filename: str = "geo-research-invitation-mail-merge.docx") -> tuple[Path, str]:
    """Create a reusable Word email body with real MERGEFIELD instructions.

    Word's email-merge dialog supplies the To field and subject. The body is
    composed from paragraph merge fields rather than relying on Word to treat
    an ``email_body`` column specially. Empty paragraph fields are harmless,
    allowing one template to serve small and large batches.
    """
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    def field(paragraph, name: str) -> None:
        run = paragraph.add_run()
        begin = OxmlElement("w:fldChar"); begin.set(qn("w:fldCharType"), "begin")
        instruction = OxmlElement("w:instrText"); instruction.set(qn("xml:space"), "preserve"); instruction.text = f" MERGEFIELD {name} \\* MERGEFORMAT "
        separate = OxmlElement("w:fldChar"); separate.set(qn("w:fldCharType"), "separate")
        result = OxmlElement("w:t"); result.text = f"«{name}»"
        end = OxmlElement("w:fldChar"); end.set(qn("w:fldCharType"), "end")
        run._r.extend((begin, instruction, separate, result, end))

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    for index in range(1, 33):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(8)
        field(paragraph, f"body_paragraph_{index:02d}")
    # The approved export places the personalized URL in the corresponding
    # paragraph field, so it is merged exactly once and is not duplicated.
    document.core_properties.title = "GEO Research Invitation Mail Merge"
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    temporary = directory / f".{path.name}.tmp"
    document.save(temporary)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    os.replace(temporary, path)
    return path, digest


def save_merged_invitation_preview(directory: Path, rows: list[dict]) -> Path:
    """Materialize the persisted messages alongside the reusable merge template."""
    from docx import Document
    from docx.shared import Pt
    document = Document()
    document.styles["Normal"].font.name = "Calibri"
    document.styles["Normal"].font.size = Pt(11)
    for index, row in enumerate(rows):
        if index:
            document.add_page_break()
        document.add_paragraph(row["company_name"], "Title")
        document.add_paragraph("To: " + row["recipient_email"])
        document.add_paragraph("Subject: " + row["email_subject"])
        document.add_paragraph("Invitation reference: " + row["invitation_reference"])
        for text in row["email_body"].split("\n\n"):
            paragraph = document.add_paragraph(text)
            paragraph.paragraph_format.space_after = Pt(6)
    target = Path(directory) / "generated-invitations.docx"
    document.save(target)
    return target


def record_word_mail_merge_reconciliation(registry_db: Path, draft_id: str, *, status: str,
                                          evidence: str, reviewer: str) -> None:
    """Record human evidence after Word/Outlook; never infer delivery from export.

    ``SENT_CONFIRMED`` requires an operator-provided Outlook evidence note.
    Exporting, previewing, or placing messages in an outbox only records the
    corresponding non-delivery status.
    """
    allowed = {"EXPORTED_PREPARED", "MERGED_TO_OUTBOX", "SENT_CONFIRMED", "FAILED", "UNKNOWN"}
    if status not in allowed or not evidence.strip() or not reviewer.strip():
        raise ValueError("reconciliation requires a valid status, evidence, and reviewer")
    from outreach_agent.research_lifecycle import init_registry, _connect
    init_registry(Path(registry_db))
    with _connect(Path(registry_db)) as db:
        draft = db.execute("SELECT draft_id,invitation_id FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        if not draft:
            raise KeyError(draft_id)
        db.execute("""CREATE TABLE IF NOT EXISTS mail_merge_reconciliations(
          reconciliation_id TEXT PRIMARY KEY,draft_id TEXT NOT NULL,status TEXT NOT NULL,
          evidence TEXT NOT NULL,reviewer TEXT NOT NULL,created_at TEXT NOT NULL,
          UNIQUE(draft_id,status,evidence)
        )""")
        reconciliation_id = "merge-reconcile-" + hashlib.sha256((draft_id + status + evidence).encode()).hexdigest()[:20]
        db.execute("INSERT OR IGNORE INTO mail_merge_reconciliations VALUES (?,?,?,?,?,?)", (reconciliation_id, draft_id, status, evidence, reviewer, datetime.now(timezone.utc).isoformat()))
        if status == "SENT_CONFIRMED":
            db.execute("UPDATE questionnaire_invitations SET status='SENT' WHERE invitation_id=?", (draft["invitation_id"],))
