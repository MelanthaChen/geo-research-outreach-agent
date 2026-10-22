from pathlib import Path

from docx import Document

from outreach_agent.reporting import generate_docx_report


def test_docx_generation_from_integration_fixture(tmp_path):
    template = Path("templates/company_geo_analysis_report_template.docx")
    output = tmp_path / "report.docx"
    result = {
        "contract_version": "geo-research-integration/v1",
        "source_profile_id": "fixture-company-1",
        "source_profile_version": "geo-company-profile/v1",
        "property_id": 77,
        "website_evidence": [{"url": "https://example.test", "content_hash": "sha256:fixture"}],
        "strategies": [{"rank": 1, "strategy_id": "s1", "recommendation_id": "r1"}],
        "baseline": {"status": "PENDING", "results_present": False},
        "limitations": ["No post-deployment result supplied"],
    }
    generate_docx_report(result, output, template)
    assert output.exists()
    text = "\n".join(paragraph.text for paragraph in Document(output).paragraphs)
    assert "Observed website evidence" in text
    assert "Proposed optimizations" in text
    assert "Real baseline measurements" in text
    assert "Post-deployment measurements" in text
    assert "Not supplied" in text

