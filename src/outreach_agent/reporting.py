"""Evidence-preserving, readable DOCX reports for GEO research results."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _text(value: Any, default: str = "Not established") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _clear_document(doc: Any) -> None:
    from docx.oxml.ns import qn
    body = doc._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _set_cell(cell: Any, value: str, *, bold: bool = False) -> None:
    from docx.shared import Pt
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.paragraph_format.space_after = 0
    run = paragraph.add_run(value)
    run.bold = bold
    run.font.size = Pt(9)


def _format_table(table: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import RGBColor
    table.style = "Table Grid"
    for row_index, row in enumerate(table.rows):
        for cell in row.cells:
            cell.vertical_alignment = 1
            tc_pr = cell._tc.get_or_add_tcPr()
            borders = tc_pr.first_child_found_in("w:tcBorders")
            if borders is None:
                borders = OxmlElement("w:tcBorders")
                tc_pr.append(borders)
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
                tag = "w:" + edge
                element = borders.find(qn(tag))
                if element is None:
                    element = OxmlElement(tag)
                    borders.append(element)
                element.set(qn("w:val"), "single")
                element.set(qn("w:sz"), "4")
                element.set(qn("w:color"), "D9D9D9")
        if row_index == 0:
            for cell in row.cells:
                shading = OxmlElement("w:shd")
                shading.set(qn("w:fill"), "2F3B35")
                cell._tc.get_or_add_tcPr().append(shading)
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
                        run.font.color.rgb = RGBColor(255, 255, 255)


def _add_table(doc: Any, headers: list[str], rows: list[list[str]]) -> Any:
    table = doc.add_table(rows=1, cols=len(headers))
    table.autofit = False
    from docx.shared import Inches
    widths = {
        2: [Inches(1.55), Inches(5.05)],
        4: [Inches(1.35), Inches(1.65), Inches(1.0), Inches(2.6)],
        5: [Inches(1.2), Inches(2.0), Inches(.8), Inches(1.7), Inches(1.9)],
    }.get(len(headers), [Inches(6.6/len(headers))]*len(headers))
    for i, header in enumerate(headers):
        table.rows[0].cells[i].width = widths[i]
        _set_cell(table.rows[0].cells[i], header, bold=True)
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    header_properties = table.rows[0]._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    header_properties.append(repeat)
    for row in rows:
        cells = table.add_row().cells
        for i, value in enumerate(row):
            cells[i].width = widths[i]
            _set_cell(cells[i], _text(value))
        properties = table.rows[-1]._tr.get_or_add_trPr()
        properties.append(OxmlElement("w:cantSplit"))
    _format_table(table)
    doc.add_paragraph()
    return table


def generate_docx_report(result: dict[str, Any], output: Path, template: Path) -> Path:
    """Render a structured result envelope without placeholders or raw JSON."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    required = ("contract_version", "source_profile_id", "source_profile_version", "property_id")
    missing = [key for key in required if key not in result]
    if missing:
        raise ValueError(f"missing result envelope fields: {', '.join(missing)}")
    evidence = result.get("observed_pages") or []
    evidence_complete = bool(evidence)
    legacy_evidence = result.get("website_evidence") or result.get("observed_website_evidence") or []
    if not evidence and legacy_evidence:
        # Backward-compatible import path for older envelopes. These records
        # are rendered as incomplete evidence, never as a completed audit.
        evidence = [
            {
                "url": item.get("url"),
                "title": "Not established",
                "headings": [],
                "exact_quotes": ["Not established from the supplied envelope"],
            }
            for item in legacy_evidence
            if isinstance(item, dict) and item.get("url")
        ]
    if not evidence:
        # Delivery previews and older integration envelopes must remain
        # renderable, but they are explicitly incomplete rather than being
        # presented as completed research reports.
        evidence = [{
            "url": "Not established",
            "title": "Not established",
            "headings": [],
            "exact_quotes": ["No rendered page evidence was supplied."],
        }]
    baseline = result.get("baseline") or {}
    baseline_complete = bool(baseline.get("results")) and baseline.get("provider") not in {None, "CONTROLLED_FIXTURE"}

    doc = Document(str(template))
    _clear_document(doc)
    from docx.shared import Inches, Pt
    for section in doc.sections:
        section.top_margin = Inches(.65)
        section.bottom_margin = Inches(.65)
        section.left_margin = Inches(.7)
        section.right_margin = Inches(.7)
    normal = doc.styles["Normal"]
    normal.font.name = "Aptos"
    normal.font.size = Pt(9)
    normal.paragraph_format.space_after = Pt(5)
    title = doc.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title.add_run(_text(result.get("report_title"), "Independent GEO Analysis Report"))
    subtitle = doc.add_paragraph()
    subtitle.add_run("Evidence-backed website audit and GEO research findings").italic = True
    doc.add_paragraph(
        "This report presents the GEO Engine evidence and findings for the website identified below. "
        "Recommendations are research hypotheses and do not promise a measured improvement. "
        "Values not supplied by the source envelope are marked Not established; legacy fields marked Not supplied are not silently inferred."
    )
    for paragraph in doc.paragraphs:
        if paragraph.style.name.startswith("Heading") or paragraph.style.name == "Title":
            paragraph.paragraph_format.keep_with_next = True
    if not evidence_complete or not baseline_complete:
        missing_parts = []
        if not evidence_complete:
            missing_parts.append("rendered page evidence")
        if not baseline_complete:
            missing_parts.append("a real AI visibility baseline")
        doc.add_paragraph(
            "REPORT STATUS: INCOMPLETE. Missing: " + ", ".join(missing_parts) + ". "
            "This document is a bounded analysis/preview and must not be treated as a completed research report."
        )

    doc.add_heading("Research scope", level=1)
    _add_table(doc, ["Field", "Verified value"], [
        ["Site", _text(result.get("site"))],
        ["Profile ID", _text(result.get("source_profile_id"))],
        ["GEO Engine property", _text(result.get("property_id"))],
        ["Evidence captured", _text(result.get("retrieved_at"))],
        ["Consent boundary", "Verified owner consent; public website analysis only"],
        ["Structured data", _text(result.get("structured_data_summary"))],
    ])

    doc.add_heading("Observed website evidence", level=1)
    doc.add_paragraph(
        (f"Chromium rendered {len(evidence)} bounded pages. Evidence below is transcribed from the rendered DOM; "
         "quotes are source text, not generated copy." if evidence_complete else
         "No rendered page evidence was supplied. The row below records the missing evidence explicitly.")
    )
    audit_rows = []
    for page in evidence:
        quotes = "\n".join(f'“{quote}”' for quote in page.get("exact_quotes", []))
        audit_rows.append([_text(page.get("url")), _text(page.get("title")), "\n".join(page.get("headings", [])), quotes])
    _add_table(doc, ["Page URL", "Title", "Rendered headings", "Observed source text"], audit_rows)

    doc.add_heading("Proposed optimizations", level=1)
    doc.add_paragraph(
        "Recommendations are grounded in the completed GEO Engine Audit and Opportunity listed below. "
        "Generated revisions are research hypotheses and should be reviewed before any website change."
    )
    rec_rows = []
    for item in result.get("recommendations") or []:
        proposed = item.get("proposed_revision")
        if not isinstance(proposed, str) or not proposed.strip() or len(proposed.strip()) > 400:
            proposed = "Proposed wording not established"
        excerpt = item.get("original_quote")
        if isinstance(excerpt, str) and len(excerpt) > 500:
            excerpt = "Long source excerpt omitted; see the URL and evidence identifier."
        if not excerpt:
            excerpt = "No short exact excerpt was stored."
        evidence_ids = item.get("supporting_evidence_ids") or []
        rec_rows.append([
            f"{item.get('rank')}. {item.get('title')}",
            f"Page: {item.get('page_url')}\nEvidence IDs: {', '.join(map(str, evidence_ids)) or 'Not supplied'}\nSource excerpt: “{excerpt}”",
            proposed,
            item.get("rationale"),
        ])
    _add_table(doc, ["Rank and recommendation", "Page evidence", "Proposed revision", "Why it is supported"], rec_rows)

    doc.add_heading("Research sources", level=1)
    doc.add_paragraph("Retrieved source records are provenance, not proof that a model answer cited or relied on a source. Citation/use is listed only when present in the persisted response.")
    source_rows = []
    for source in result.get("research_sources") or []:
        source_rows.append([
            f"[{source.get('rank')}] {source.get('title') or 'Title not available'}",
            source.get("url") or "URL not available",
            source.get("source_type") or "Type not established",
            source.get("relevance") or "Retrieved; no specific use established",
            source.get("evidence_id") or source.get("content_sha256") or "Evidence identifier unavailable",
        ])
    if source_rows:
        _add_table(doc, ["Source", "URL", "Type", "Finding / use", "Evidence ID"], source_rows)
    else:
        doc.add_paragraph("Source registry was not included in the stored result envelope; no source citations are inferred.")

    model_results = [item for item in (result.get("baseline") or {}).get("results", [])
                     if isinstance(item, dict) and isinstance(item.get("response"), str) and item.get("response").strip()]
    if model_results:
        doc.add_heading("Model-generated interpretations", level=1)
        doc.add_paragraph("The following are GEO Engine model outputs for the persisted research query. They are not answers retrieved from third-party AI search products or evidence of external search rankings.")
        for item in model_results:
            label = str(item.get("strategy") or "strategy")
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.keep_together = True
            run = paragraph.add_run(label.title() + " strategy: ")
            run.bold = True
            paragraph.add_run(item["response"].strip())

    doc.add_heading("Research questions", level=1)
    _add_table(doc, ["Question", "Evidence basis"], [[q, "Derived from rendered page content and stated demo scope"] for q in result.get("questions") or []])

    baseline_heading = doc.add_heading("Real baseline measurements", level=1)
    baseline_heading.paragraph_format.page_break_before = True
    baseline = result.get("baseline") or {}
    _add_table(doc, ["Measure", "Observed value"], [
        ["Provider", _text(baseline.get("provider"), "Not run")],
        ["Model", _text(baseline.get("model"), "Not established")],
        ["Model calls", _text(baseline.get("model_calls"), "0")],
        ["Response archive", _text(baseline.get("response_archive"), "Not available")],
        ["Strategy results", "; ".join(
            f"{item.get('strategy', 'strategy')}: {item.get('metrics', {}).get('word_count', 'not recorded')} cited words, "
            f"{item.get('metrics', {}).get('citation_count', 'not recorded')} citations"
            for item in baseline.get("results", []) if isinstance(item, dict)
        ) or "Not measured"],
        ["Interpretation", _text(baseline.get("interpretation"), "No external AI-search visibility claim is established.")],
    ])

    doc.add_heading("Post-deployment measurements", level=1)
    doc.add_paragraph("No website changes were deployed. Post-optimization measurements are Not measured. The proposed revisions are hypotheses for a separately approved experiment.")

    doc.add_heading("Limitations and next steps", level=1)
    for limitation in result.get("limitations", []):
        doc.add_paragraph(_text(limitation), style="List Bullet")
    doc.add_paragraph("Next step: review the audit-backed recommendations and model-generated revisions before deciding whether to change the website. No website changes or external AI-search ranking measurements are included in this report.")
    doc.add_paragraph("Research integrity note. The report distinguishes rendered website evidence, existing Audit recommendations, live retrieval sources, and persisted model-generated responses. GEO Engine model evaluations do not establish rankings in third-party AI search products.")

    for paragraph in doc.paragraphs:
        if paragraph.style.name.startswith("Heading") or paragraph.style.name == "Title":
            paragraph.paragraph_format.keep_with_next = True

    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    return output
