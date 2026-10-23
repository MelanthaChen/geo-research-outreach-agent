from pathlib import Path

from outreach_agent.geo_result_delivery import import_geo_result, persist_geo_result_preview, report_email_preview
from outreach_agent.research_lifecycle import _connect, init_registry


def test_geo_result_preview_is_persisted_and_idempotent(tmp_path):
    result = {
        "contract_version": "geo-research-integration/v1",
        "source_profile_id": "fixture-company",
        "source_profile_version": "v1",
        "property_id": 41,
        "record_id": "research-result:fixture-company:v1",
        "observed_website_evidence": [{"url": "https://fixture.example", "text": "Observed fixture evidence"}],
        "strategies": [{"rank": 1, "strategy_id": "entity_clarity"}],
        "baseline": {"provider": "CONTROLLED_FIXTURE", "results": []},
        "limitations": ["No paid provider call was made"],
    }
    template = Path(__file__).parents[1] / "templates" / "company_geo_analysis_report_template.docx"
    first = persist_geo_result_preview(result, path=tmp_path / "registry.db", report_directory=tmp_path / "reports", template=template)
    again = persist_geo_result_preview(result, path=tmp_path / "registry.db", report_directory=tmp_path / "reports", template=template)
    assert first["status"] == "CREATED"
    assert again["status"] == "EXISTING"
    assert first["report_id"] == again["report_id"]
    assert Path(first["report_path"]).exists()
    preview = report_email_preview(result, recipient="official@example.test")
    assert preview["status"] == "PREVIEW_ONLY"
    assert "No paid provider call was made" in preview["body"]


def test_geo_result_import_requires_handoff_and_is_idempotent(tmp_path):
    path = tmp_path / "registry.db"
    init_registry(path)
    with _connect(path) as db:
        db.execute(
            "INSERT INTO research_records(profile_id,profile_version,profile_type,name,official_website,consent_status,geo_property_id,geo_research_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("fixture-company", "v1", "owner_controlled_test", "Fixture", "https://fixture.example", "CONSENTED", 41, "HANDED_OFF", "now", "now"),
        )
        db.execute(
            "INSERT INTO geo_handoffs(handoff_id,profile_id,profile_version,contract_version,consent_submission_id,scope_json,status,property_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("handoff-1", "fixture-company", "v1", "geo-research-integration/v1", "submission-1", "{}", "HANDED_OFF", 41, "now", "now"),
        )
    result = {
        "contract_version": "geo-research-integration/v1", "record_type": "research_result_export",
        "schema_version": "geo-research-integration/v1", "record_id": "research-result:fixture-company:v1",
        "source_profile_id": "fixture-company", "source_profile_version": "v1", "property_id": 41,
        "audit_reference": {}, "strategies": [], "question_set_reference": None,
        "baseline_reference": None, "post_deployment_reference": None,
        "exported_at": "2026-10-23T00:00:00+00:00", "provenance": {"source_system": "geo-engine"},
        "limitations": ["controlled fixture"],
    }
    template = Path(__file__).parents[1] / "templates/company_geo_analysis_report_template.docx"
    first = import_geo_result(result, path=path, report_directory=tmp_path / "reports", template=template)
    again = import_geo_result(result, path=path, report_directory=tmp_path / "reports", template=template)
    assert first["status"] == "CREATED"
    assert again["status"] == "EXISTING"
    with _connect(path) as db:
        row = db.execute("SELECT geo_property_id,geo_research_status FROM research_records WHERE profile_id=?", ("fixture-company",)).fetchone()
        assert row["geo_property_id"] == 41
        assert row["geo_research_status"] == "REPORT_READY"


def test_geo_result_import_rejects_property_mismatch(tmp_path):
    path = tmp_path / "registry.db"
    init_registry(path)
    with _connect(path) as db:
        db.execute(
            "INSERT INTO research_records(profile_id,profile_version,profile_type,name,official_website,consent_status,geo_property_id,geo_research_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("fixture-company", "v1", "owner_controlled_test", "Fixture", "https://fixture.example", "CONSENTED", 41, "HANDED_OFF", "now", "now"),
        )
        db.execute(
            "INSERT INTO geo_handoffs(handoff_id,profile_id,profile_version,contract_version,consent_submission_id,scope_json,status,property_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("handoff-1", "fixture-company", "v1", "geo-research-integration/v1", "submission-1", "{}", "HANDED_OFF", 41, "now", "now"),
        )
    result = {"contract_version":"geo-research-integration/v1","record_type":"research_result_export","schema_version":"geo-research-integration/v1","record_id":"r","source_profile_id":"fixture-company","source_profile_version":"v1","property_id":99,"audit_reference":{},"strategies":[],"question_set_reference":None,"baseline_reference":None,"post_deployment_reference":None,"exported_at":"2026-10-23T00:00:00+00:00","provenance":{}}
    template = Path(__file__).parents[1] / "templates/company_geo_analysis_report_template.docx"
    import pytest
    with pytest.raises(ValueError, match="property"):
        import_geo_result(result, path=path, report_directory=tmp_path / "reports", template=template)
