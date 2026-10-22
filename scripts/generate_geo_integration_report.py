"""Render a validated GEO Engine integration envelope to DOCX."""

import argparse
import json
from pathlib import Path

from outreach_agent.reporting import generate_docx_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_json", type=Path)
    parser.add_argument("output_docx", type=Path)
    parser.add_argument("--template", type=Path, default=Path("templates/company_geo_analysis_report_template.docx"))
    args = parser.parse_args()
    result = json.loads(args.result_json.read_text(encoding="utf-8"))
    print(generate_docx_report(result, args.output_docx, args.template))


if __name__ == "__main__":
    main()

