from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from outreach_v2 import auth
from outreach_v2.app import create_app
from outreach_v2.db import Database, batches, invitations, jobs, submissions, sessions, now
from outreach_v2.demo import PREVIEW_REFERENCE, build_preview_package, lagoon_case


def test_saved_lagoon_artifacts_integrity_and_quality():
    case = lagoon_case()
    assert case["company"] == "Lagoon New York"
    assert case["fixture"] is False
    assert case["persisted_invitation"] is False
    assert case["research_completed"] is False
    assert case["request_id"] == "resp_0db4c56e8cc63a3c016ae27b61120c87d28fdc373c94579af3"
    assert case["quality_decisions"]["EVIDENCE_VALID"]["passed"] is True
    assert case["quality_decisions"]["OUTREACH_REVIEW_READY"]["passed"] is True
    assert {item["status"] for item in case["claim_copy_audit"]} >= {
        "EXCLUDED_UNSUPPORTED_COMPARISON", "REPLACED_WITH_DIRECT_SOURCE_DESCRIPTION",
        "RECOMPOSED_FROM_CITED_SOURCE_FACTS",
    }
    assert all(item["exact_excerpt_matches_source"] for item in case["source_excerpt_checks"])


def test_lagoon_demo_routes_require_admin_and_do_not_write_lifecycle_rows(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'demo.sqlite'}")
    db.initialize()
    token, csrf = "demo-session-token", "demo-csrf-token"
    with db.transaction() as conn:
        conn.execute(sessions.insert().values(token_hash=auth.digest(token), csrf_hash=auth.digest(csrf),
            email="admin@example.test", expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            created_at=now()))
    app = create_app(database=db)
    with TestClient(app) as client:
        assert client.get("/api/demo/lagoon").status_code == 401
        before = {}
        with db.transaction() as conn:
            for table in (invitations, batches, submissions, jobs):
                before[table.name] = conn.scalar(select(func.count()).select_from(table))
        response = client.get("/api/demo/lagoon", cookies={"outreach_v2_session": token})
        assert response.status_code == 200
        payload = response.json()
        assert payload["case_status"] == "REVIEW PREVIEW / NOT SENT"
        assert payload["reference"] == PREVIEW_REFERENCE
        package = client.get("/api/demo/lagoon/package", cookies={"outreach_v2_session": token})
        assert package.status_code == 200
        assert package.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
            assert set(archive.namelist()) == {
                "REVIEW-PREVIEW-NOT-SENT.docx", "invitation-preview.txt",
                "evidence-and-validation.json", "model-run-metadata.json",
                "raw-model-response.json", "manifest.json", "README-NOT-FOR-SENDING.txt",
            }
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["sendable"] is False
            assert manifest["persisted_invitation"] is False
            assert manifest["reference"] == PREVIEW_REFERENCE
            assert "GEO-A67A" not in archive.read("invitation-preview.txt").decode()
        with db.transaction() as conn:
            after = {table.name: conn.scalar(select(func.count()).select_from(table))
                     for table in (invitations, batches, submissions, jobs)}
        assert after == before


def test_preview_package_contains_the_saved_email_and_no_live_reference():
    case = lagoon_case()
    with zipfile.ZipFile(io.BytesIO(build_preview_package(case))) as archive:
        text = archive.read("invitation-preview.txt").decode()
        assert case["body"] in text
        assert PREVIEW_REFERENCE in text
        assert "GEO-" not in text.replace("PREVIEW-NOT-SENDABLE", "")
        assert json.loads(archive.read("manifest.json"))["new_model_request_made"] is False
