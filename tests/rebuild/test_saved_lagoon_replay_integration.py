"""Regression-test the already-saved Lagoon response without provider access or DB writes."""
import json
from pathlib import Path

from outreach_v2.quality import quality_decisions
from outreach_v2.template import render, validate


def test_saved_real_lagoon_replay_passes_current_gate_and_application_renderer():
    root = Path(__file__).resolve().parents[2]
    artifact = root / "artifacts" / "semantic-poc" / "lagoon-openai-replay.json"
    if not artifact.is_file():
        artifact = root / "src" / "outreach_v2" / "demo_data" / "lagoon-openai-replay.json"
    replay = json.loads(artifact.read_text(encoding="utf-8"))
    assert replay["replayed_saved_response"] is True
    assert replay["new_model_request_made"] is False
    personalization = replay["final_personalization"]
    company = "Lagoon New York"
    website = "https://www.lagoonny.com/"
    record = {"id": "lagoon-preview-test", "name": company, "website": website, "fixture": False}

    validate(personalization, website)
    decisions = quality_decisions(personalization, record=record, candidate_name=company, website=website)
    assert decisions["EVIDENCE_VALID"]["passed"]
    assert decisions["OUTREACH_REVIEW_READY"]["passed"]
    assert decisions["CLAIM_GROUNDING"]["status"] == "AUTOMATED_FILTERING"

    subject, body = render(company, website, personalization,
        "https://example.invalid/not-a-live-form", "PREVIEW-NOT-SEND", record=record,
        identity={"researcher_name":"Yuxuan Chen","university_affiliation":"Northeastern University",
                  "reply_to":"chen.yuxuan9@northeastern.edu"})
    assert subject and company in body
    for value in personalization["recipient_copy"].values():
        if isinstance(value, str) and value:
            assert value in body
    assert "OWNER-CONTROLLED STAGING TEST" not in body
    assert "PREVIEW-NOT-SEND" in body
