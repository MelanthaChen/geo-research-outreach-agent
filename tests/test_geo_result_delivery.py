from pathlib import Path

from outreach_agent.geo_result_delivery import persist_geo_result_preview, report_email_preview


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
