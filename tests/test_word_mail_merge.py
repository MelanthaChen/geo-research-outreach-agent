import sqlite3

from outreach_agent.mail_merge import (
    RESEARCH_MAIL_MERGE_COLUMNS, record_word_mail_merge_reconciliation,
    research_mail_merge_rows, save_research_mail_merge_xlsx,
    save_word_mail_merge_template,
)
from outreach_agent.outreach_pipeline import mail_merge_safe_subject
from outreach_agent.research_lifecycle import create_email_draft, create_questionnaire_invitation, register_profile


def profile():
    return {"schema_version": "geo-company-profile/v1", "profile_id": "word-company", "profile_type": "owner_controlled_test", "identity": {"name": "GeoAIResume", "website": "https://geo-ai-resume.vercel.app"}, "startup": {"location": "Boston, MA"}}


def test_pending_preview_is_not_sendable_and_xlsx_preserves_form_url(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("word-company", secret="secret", path=db)
    draft = create_email_draft("word-company", "yuxuan.chen.1031@outlook.com", "Form https://example.test/form?ref=GEO-TEST", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=? WHERE draft_id=?", (invitation_id, draft))
    rows = research_mail_merge_rows(db, profile_ids=["word-company"], include_pending=True, allowed_recipients={"yuxuan.chen.1031@outlook.com"})
    assert len(rows) == 1 and rows[0]["merge_status"] == "REVIEW_ONLY_NOT_APPROVED"
    assert rows[0]["questionnaire_url"] == "https://example.test/form?ref=GEO-TEST"
    path, digest = save_research_mail_merge_xlsx(tmp_path, "geoairesume-mail-merge.xlsx", rows)
    assert path.exists() and len(digest) == 64
    from openpyxl import load_workbook
    sheet = load_workbook(path, read_only=True).active
    values = sheet.values
    assert tuple(next(values)) == RESEARCH_MAIL_MERGE_COLUMNS
    assert next(values)[2] == "yuxuan.chen.1031@outlook.com"

    template, _ = save_word_mail_merge_template(tmp_path)
    from zipfile import ZipFile
    with ZipFile(template) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert xml.count("MERGEFIELD") == 32
    assert "body_paragraph_01" in xml and "body_paragraph_32" in xml


def test_approved_export_and_reconciliation_do_not_assume_delivery(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("word-company", secret="secret", path=db)
    draft = create_email_draft("word-company", "yuxuan.chen.1031@outlook.com", "Form https://example.test/form", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE email_drafts SET invitation_id=?,approval_status='APPROVED' WHERE draft_id=?", (invitation_id, draft))
    assert research_mail_merge_rows(db)[0]["merge_status"] == "READY_FOR_WORD_MAIL_MERGE"
    record_word_mail_merge_reconciliation(db, draft, status="EXPORTED_PREPARED", evidence="XLSX hash recorded", reviewer="human")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM questionnaire_invitations").fetchone()[0] == "INVITED"
    record_word_mail_merge_reconciliation(db, draft, status="SENT_CONFIRMED", evidence="Outlook Sent Items message ID reviewed", reviewer="human")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM questionnaire_invitations").fetchone()[0] == "SENT"


def test_word_export_normalizes_typographic_subject_without_resending(tmp_path):
    db = tmp_path / "registry.db"
    register_profile(profile(), db)
    invitation_id, _ = create_questionnaire_invitation("word-company", secret="secret", path=db)
    draft = create_email_draft(
        "word-company",
        "yuxuan.chen.1031@outlook.com",
        "https://example.test/form",
        path=db,
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE email_drafts SET invitation_id=?, subject=?, approval_status='APPROVED' WHERE draft_id=?",
            (invitation_id, "Controlled GEO research test — GeoAIResume", draft),
        )
    rows = research_mail_merge_rows(db)
    assert rows[0]["email_subject"] == "Controlled GEO research test - GeoAIResume"
    assert rows[0]["merge_status"] == "READY_FOR_WORD_MAIL_MERGE"
    assert mail_merge_safe_subject("A — B … C") == "A - B ... C"
