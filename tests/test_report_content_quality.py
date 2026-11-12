from pathlib import Path
from docx import Document

from outreach_agent.reporting import generate_docx_report
from scripts.docx_report_qa import validate_intermediate_page_density


def test_geoairesume_report_keeps_full_page_out_of_proposed_revision_and_lists_sources(tmp_path):
    huge_page = "GeoAIResume homepage paragraph " * 3000
    result = {
        "contract_version": "geo-research-integration/v1", "source_profile_id":"geoairesume-owner-test-v1",
        "source_profile_version":"inv-353e5922aa8e4686ad82ff99c6d0c91d", "property_id":2,
        "site":"https://geo-ai-resume.vercel.app/", "experiment_summary":{"experiment_id":8},
        "observed_pages":[{"url":"https://geo-ai-resume.vercel.app/","title":"GeoAIResume",
            "headings":["Resume research"],"exact_quotes":["The saved excerpt is a short source fact."],
            "content_sha256":"a"*64}],
        "recommendations":[{"rank":1,"title":"Add missing meta descriptions",
            "page_url":"https://geo-ai-resume.vercel.app/","original_quote":"Short exact quote",
            "proposed_revision":huge_page,"rationale":"Recommendation rationale.",
            "supporting_evidence_ids":["experiment:8:document:1"]}],
        "research_sources":[{"rank":1,"title":"GeoAIResume target page","url":"https://geo-ai-resume.vercel.app/",
            "source_type":"target website","relevance":"Cited in persisted strategy response(s): original, faq",
            "evidence_id":"experiment:8:document:1","content_sha256":"a"*64}],
        "baseline":{"provider":"openai","model":"gpt-6-luna","model_calls":2,
            "results":[{"strategy":"original","response":"The target page says this [1].","metrics":{"word_count":61,"citation_count":4}},
                      {"strategy":"faq","response":"The FAQ result says this [1].","metrics":{"word_count":53,"citation_count":3}}]},
        "questions":["What does the page say?"],"limitations":["No external rankings are measured."],
    }
    template = Path(__file__).parents[1] / "templates/company_geo_analysis_report_template.docx"
    output = generate_docx_report(result, tmp_path/"geo.docx", template)
    doc = Document(output)
    all_text = "\n".join([p.text for p in doc.paragraphs] + [cell.text for table in doc.tables for row in table.rows for cell in row.cells])
    assert "Research sources" in all_text
    assert "Model-generated interpretations" in all_text
    assert "not answers retrieved from third-party AI search products" in all_text
    assert "Evidence IDs: experiment:8:document:1" in all_text
    assert "Proposed wording not established" in all_text
    assert "Retrieved source records are provenance" in all_text
    assert "GeoAIResume homepage paragraph" not in all_text
    assert "https://geo-ai-resume.vercel.app/" in all_text
    assert "Persisted model response archive" not in all_text
    assert "original: 61 cited words" in all_text
    assert len(list(doc.tables)) >= 5
    limitation_index = next(i for i, p in enumerate(doc.paragraphs) if p.text == "Limitations and next steps")
    limitation_bullets = [p for p in doc.paragraphs[limitation_index + 1:] if p.style.name == "List Bullet"]
    assert len(limitation_bullets) == 1
    assert limitation_bullets[0].text.endswith("Review the recommendations before making any website change.")


def test_baseline_section_does_not_force_a_new_page(tmp_path):
    result = {
        "contract_version": "geo-research-integration/v1", "source_profile_id": "geoairesume-owner-test-v1",
        "source_profile_version": "inv-353e5922aa8e4686ad82ff99c6d0c91d", "property_id": 2,
        "site": "https://geo-ai-resume.vercel.app/",
        "observed_pages": [{"url": "https://geo-ai-resume.vercel.app/", "title": "GeoAIResume",
            "headings": ["Resume research"], "exact_quotes": ["Saved first-party evidence."]}],
        "questions": ["What does the page state?"],
        "baseline": {"provider": "openai", "model": "gpt-6-luna", "model_calls": 2,
            "results": [{"strategy": "original", "response": "The page says this [1].",
                         "metrics": {"word_count": 4, "citation_count": 1}}]},
    }
    template = Path(__file__).parents[1] / "templates/company_geo_analysis_report_template.docx"
    output = generate_docx_report(result, tmp_path / "flowing-baseline.docx", template)
    heading = next(p for p in Document(output).paragraphs if p.text == "Real baseline measurements")
    assert heading.paragraph_format.page_break_before is not True


def test_render_qa_rejects_nearly_empty_intermediate_page():
    import pytest

    with pytest.raises(ValueError, match=r"page 2 is nearly empty"):
        validate_intermediate_page_density([
            "Substantive first page. " * 40,
            "Research questions\nQuestion\tEvidence basis\nOne short question\tNot established",
            "Substantive final page. " * 40,
        ])


def test_render_qa_rejects_stranded_short_final_page():
    import pytest

    with pytest.raises(ValueError, match=r"page 3 is nearly empty"):
        validate_intermediate_page_density([
            "Substantive first page. " * 40,
            "Substantive middle page. " * 40,
            "Short concluding note.",
        ])
