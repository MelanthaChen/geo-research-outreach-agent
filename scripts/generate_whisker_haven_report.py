"""Build the durable Whisker Haven independent-analysis report."""

import json
from pathlib import Path

from outreach_agent.reporting import generate_docx_report

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "data/whisker_haven_independent_analysis_20261023.json"
RESULT = Path("/tmp/whisker-independent-v2-result.json")
TEMPLATE = ROOT / "templates/company_geo_analysis_report_template.docx"
OUTPUT = ROOT / "demo_runs/whisker_haven_20261023/Whisker-Haven-GEO-Analysis.docx"


def main() -> None:
    evidence = json.loads(EVIDENCE.read_text())
    cloud = json.loads(RESULT.read_text())
    result = cloud["result"]
    audit = result.get("audit_reference") or {}
    baseline = result.get("baseline_reference") or {}
    baseline_results = baseline.get("results") or {}
    report = {
        "contract_version": result["contract_version"],
        "source_profile_id": result["source_profile_id"],
        "source_profile_version": result["source_profile_version"],
        "property_id": result["property_id"],
        "report_title": "Whisker Haven Independent GEO Analysis",
        "site": evidence["site"],
        "retrieved_at": evidence["retrieved_at"],
        "structured_data_summary": "No JSON-LD blocks observed in the five rendered pages.",
        "observed_pages": evidence["pages"],
        "recommendations": [
            {"rank": 1, "title": "Add a concise entity and scope summary on the homepage", "page_url": evidence["pages"][0]["url"], "original_quote": evidence["pages"][0]["exact_quotes"][0], "proposed_revision": "Whisker Haven is a fictional cat adoption experience for testing. It presents six fictional cat profiles and a sample adoption walkthrough; it is not a real shelter and does not process adoptions.", "rationale": "The existing homepage explains the cats but leaves the fictional/demo scope partly distributed across other pages. A concise, factual entity summary would give retrieval systems a stable description without claiming real services."},
            {"rank": 2, "title": "Give the cat directory a stable inventory summary", "page_url": evidence["pages"][1]["url"], "original_quote": "6 cats to meet", "proposed_revision": "Available fictional cats: Miso, Olive, Pippin, Juniper, Fig, and Sage. Profiles include age, personality, and whether the cat is marked ready to meet or in foster care.", "rationale": "The directory visibly contains six named profiles and status labels. A single textual inventory would make the collection and its attributes easier to quote accurately."},
            {"rank": 3, "title": "Publish a visible process and limitations FAQ", "page_url": evidence["pages"][2]["url"], "original_quote": evidence["pages"][2]["exact_quotes"][0], "proposed_revision": "Whisker Haven is a fictional walkthrough, not an adoption service. The sample sequence is: browse profiles, send a hello, have a chat, and welcome home. No real adoption application is processed.", "rationale": "The process page already describes four stages and labels the site fictional. A compact FAQ would preserve that limitation while making the page answerable for process-oriented questions."}
        ],
        "questions": [
            "What is Whisker Haven and is it a real shelter?",
            "Which fictional cats are listed on Whisker Haven?",
            "What steps does the Whisker Haven sample adoption walkthrough describe?",
            "Does Whisker Haven process real adoptions or store contact form submissions?"
        ],
        "baseline": {"provider": baseline_results.get("provider", "Not run"), "model": "Not established", "model_calls": 0, "response_archive": "Not available", "results": "Not measured", "interpretation": "The controlled fixture produced no model responses. No paid or external AI visibility calls were authorized."},
        "limitations": [
            f"Cloud audit status: {audit.get('status', 'Not established')}; the Render HTTP-first path recorded no page references because the demo is client-rendered.",
            "No JSON-LD structured data was observed in the five rendered pages.",
            "No real AI visibility provider was called: model call count 0. The existing OpenAI credential was not used because paid execution was not approved and no free provider was configured.",
            "No website change, experiment, citation test, email, or consent-gated Outreach handoff was performed.",
            "The live Outreach response remains NEEDS_REVIEW and is not treated as verified consent."
        ]
    }
    generate_docx_report(report, OUTPUT, TEMPLATE)
    print(OUTPUT)


if __name__ == "__main__":
    main()
