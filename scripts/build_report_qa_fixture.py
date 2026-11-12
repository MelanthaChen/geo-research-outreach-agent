from pathlib import Path
from tempfile import TemporaryDirectory
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from outreach_agent.reporting import generate_docx_report

root=Path(__file__).parents[1]
template=root/"templates/company_geo_analysis_report_template.docx"
result={"contract_version":"geo-research-integration/v1","source_profile_id":"qa-fixture",
 "source_profile_version":"fixture-v1","property_id":"qa","site":"https://example.test/",
 "observed_pages":[{"url":"https://example.test/","title":"QA fixture","headings":["Overview"],
   "exact_quotes":["An example page used only to exercise report formatting."],"content_sha256":"a"*64}],
 "recommendations":[{"rank":1,"title":"Improve clarity","page_url":"https://example.test/",
   "original_quote":"Short exact excerpt","proposed_revision":None,"rationale":"A bounded QA recommendation."}],
 "research_sources":[{"rank":1,"title":"QA fixture target","url":"https://example.test/","source_type":"target website",
   "relevance":"Cited in persisted strategy response(s): original","evidence_id":"fixture:source:1","content_sha256":"a"*64}],
 "baseline":{"provider":"openai","model":"offline-fixture","model_calls":1,"response_archive":"fixture-only",
   "interpretation":"Formatting fixture only.","results":[{"strategy":"original","response":"The test fixture says this [1].",
     "metrics":{"word_count":12,"citation_count":1}}]},
 "questions":["What information is available?"],"limitations":["This fixture is not research evidence."]}
out=Path("/tmp/geo-report-qa-fixture.docx")
generate_docx_report(result,out,template)
print(out)
