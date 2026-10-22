"""Evidence-preserving DOCX rendering for imported GEO Engine results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _value(value: Any) -> str:
    if value is None:
        return "Not supplied"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def generate_docx_report(result: dict[str, Any], output: Path, template: Path) -> Path:
    """Render a validated envelope using the supplied report template.

    Only fields present in ``result`` are rendered. Missing values are explicitly
    labelled, so this function cannot manufacture quotes, rankings, or lifts.
    """
    from docx import Document

    required = ("contract_version", "source_profile_id", "source_profile_version", "property_id")
    missing = [key for key in required if key not in result]
    if missing:
        raise ValueError(f"missing result envelope fields: {', '.join(missing)}")
    doc = Document(str(template))
    doc.add_page_break()
    doc.add_heading("Phase 1 integration result", level=1)
    doc.add_paragraph("This report is a rendering of imported records. It does not run audits, experiments, deployments, or outreach.")
    doc.add_paragraph(f"Source profile ID: {_value(result['source_profile_id'])}")
    doc.add_paragraph(f"Source profile version: {_value(result['source_profile_version'])}")
    doc.add_paragraph(f"GEO Engine property ID: {_value(result['property_id'])}")
    sections = [
        ("Observed website evidence", result.get("observed_website_evidence", result.get("website_evidence"))),
        ("Proposed optimizations", result.get("strategies", result.get("exact_text_edits"))),
        ("Simulated experiments", result.get("experiment")),
        ("Real baseline measurements", result.get("baseline")),
        ("Verified deployments", result.get("deployment_receipts")),
        ("Post-deployment measurements", result.get("post_deployment")),
        ("Missing data and limitations", result.get("limitations", result.get("missing_data"))),
    ]
    for title, value in sections:
        doc.add_heading(title, level=2)
        doc.add_paragraph(_value(value))
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output))
    return output

