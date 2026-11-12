#!/usr/bin/env python3
"""Structural and page-render QA for generated report DOCX files."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from docx import Document

REQUIRED = ("Research scope", "Observed website evidence", "Proposed optimizations",
            "Research sources", "Model-generated interpretations", "Research questions", "Real baseline measurements",
            "Post-deployment measurements", "Limitations and next steps")
MIN_INTERMEDIATE_PAGE_WORDS = 50
MIN_INTERMEDIATE_PAGE_CHARS = 250


def validate_intermediate_page_density(page_texts: list[str]) -> None:
    """Reject near-empty non-cover pages, including a stranded short final page."""
    for index, page in enumerate(page_texts[1:], start=2):
        normalized = page.strip()
        words = re.findall(r"\b\w+\b", normalized)
        if len(words) < MIN_INTERMEDIATE_PAGE_WORDS or len(normalized) < MIN_INTERMEDIATE_PAGE_CHARS:
            raise ValueError(
                f"Rendered page {index} is nearly empty "
                f"({len(words)} words, {len(normalized)} characters)"
            )


def check(path: Path, render: bool = True) -> int:
    doc = Document(path)
    paragraphs = [p.text for p in doc.paragraphs]
    cells = [cell.text for table in doc.tables for row in table.rows for cell in row.cells]
    text = "\n".join(paragraphs + cells)
    missing = [section for section in REQUIRED if section not in text]
    if missing:
        raise SystemExit("Missing required sections: " + ", ".join(missing))
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                value = cell.text.strip()
                if len(value) > 2500:
                    raise SystemExit(f"Anomalously long DOCX table cell ({len(value)} chars)")
                if re.search(r"\{\s*['\"](?:strategy|response|metrics|provider)['\"]\s*:", value):
                    raise SystemExit("Raw JSON-like object found in a DOCX table cell")
    if "http" not in text:
        raise SystemExit("No source URL appears in the report")
    urls = re.findall(r"https?://[^\s\]\[<>\"']+", text)
    if not urls or any(urlsplit(url.rstrip(".,;)")).scheme not in {"http", "https"} or not urlsplit(url.rstrip(".,;)")).hostname for url in urls):
        raise SystemExit("A reported source URL is malformed")
    recommendation_table = next((table for table in doc.tables if table.rows and
        "Proposed revision" in [cell.text for cell in table.rows[0].cells]), None)
    if recommendation_table and len(recommendation_table.rows) > 1:
        for row in recommendation_table.rows[1:]:
            evidence_cell = row.cells[1].text
            if "Page: https://" not in evidence_cell or "Evidence IDs:" not in evidence_cell:
                raise SystemExit("A recommendation lacks its exact page URL or evidence identifier")
    source_table = next((table for table in doc.tables if table.rows and
        "Evidence ID" in [cell.text for cell in table.rows[0].cells]), None)
    if source_table:
        source_ranks = set()
        for row in source_table.rows[1:]:
            if not row.cells[1].text.startswith("http") or not row.cells[4].text.strip():
                raise SystemExit("A research source lacks a URL or inspectable evidence identifier")
            match = re.match(r"\[(\d+)\]", row.cells[0].text)
            if match: source_ranks.add(match.group(1))
        citations = set(re.findall(r"\[(\d+)\]", text))
        if citations - source_ranks:
            raise SystemExit("A model citation has no matching research source rank")
    if render:
        with tempfile.TemporaryDirectory(prefix="report-docx-qa-") as temp:
            out = Path(temp)
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(out), str(path)],
                           check=True, capture_output=True, text=True)
            pdf = out / f"{path.stem}.pdf"
            subprocess.run(["pdftoppm", "-png", "-r", "90", str(pdf), str(out / "page")],
                           check=True, capture_output=True, text=True)
            pages = sorted(out.glob("page-*.png"))
            if not pages:
                raise SystemExit("DOCX rendered to no page images")
            info = subprocess.run(["pdfinfo", str(pdf)], check=True, capture_output=True, text=True).stdout
            count = int(re.search(r"Pages:\s+(\d+)", info).group(1))
            if count != len(pages):
                raise SystemExit("PDF page count does not match rendered page images")
            page_text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], check=True,
                                       capture_output=True, text=True).stdout.split("\f")
            if len(page_text) < count:
                raise SystemExit("Rendered PDF text page count is incomplete")
            for index, page in enumerate(page_text[:count], 1):
                if len(page.strip()) < 40:
                    raise SystemExit(f"Rendered page {index} is nearly empty")
            try:
                validate_intermediate_page_density(page_text[:count])
            except ValueError as error:
                raise SystemExit(str(error)) from error
            print(f"DOCX_RENDER_QA=PASS pages={count}")
            return count
    print("DOCX_STRUCTURAL_QA=PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: docx_report_qa.py FILE.docx [--no-render]")
    check(Path(sys.argv[1]), render=len(sys.argv) == 2)
