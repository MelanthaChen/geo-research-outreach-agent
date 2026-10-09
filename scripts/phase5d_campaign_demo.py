"""Seed/resume the local Phase 5D DEMO campaign and export its saved state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from outreach_agent.campaigns import CampaignStore, DEFAULT_CAMPAIGN_DB  # noqa: E402
from outreach_agent.dashboard_data import DEFAULT_DB, DEFAULT_DELIVERY, DEFAULT_VERIFICATION, load_dashboard_data  # noqa: E402
from outreach_agent.wikidata import write_csv_atomic  # noqa: E402

SUMMARY_FIELDS = [
    "demo_label", "campaign_id", "campaign_name", "campaign_status", "company_count", "draft_count",
    "email_draft_count", "contact_form_review_count", "excluded_count", "simulated_deliveries",
    "simulated_failures", "simulated_responses", "simulated_interest_submissions", "demo_reviewed_submissions",
    "eligible_count", "ineligible_count", "unresolved_count",
    "simulated_followups_started", "real_sends", "real_form_submissions", "formal_consents",
]
STATUS_FIELDS = [
    "demo_label", "campaign_id", "company_id", "company_name", "website", "qualification_status",
    "identity_status", "channel_type", "recipient_or_form_url", "evidence_url", "contact_review_status",
    "item_status", "delivery_status", "delivery_attempts", "simulated_response", "interest_status",
    "form_opened_at", "token_expires_at", "submission_status", "research_team_review_status",
    "simulated_handoff_status", "provider", "last_error", "source_data_mutated",
]


def export(store: CampaignStore, output_dir: Path) -> tuple[Path, Path]:
    summary_rows = []
    status_rows = []
    for listed in sorted(store.list(), key=lambda row: (row["created_at"], row["id"])):
        campaign = store.get(listed["id"])
        submission_by_item = {r["item_id"]: r for r in campaign["interest_submissions"]}
        items = campaign["items"]
        summary_rows.append({
            "demo_label": "DEMO ONLY — SYNTHETIC EVENTS; NO REAL OUTREACH",
            "campaign_id": campaign["id"], "campaign_name": campaign["name"],
            "campaign_status": campaign["status"], "company_count": campaign["company_count"],
            "draft_count": campaign["draft_count"],
            "email_draft_count": sum(bool(item["draft"]) and item["channel_type"] != "CONTACT_FORM" for item in items),
            "contact_form_review_count": campaign["contact_form_review_count"],
            "excluded_count": campaign["excluded_count"], "eligible_count": campaign["eligible_count"],
            "ineligible_count": campaign["ineligible_count"], "unresolved_count": campaign["unresolved_count"],
            "simulated_deliveries": campaign["simulated_deliveries"], "simulated_failures": campaign["simulated_failures"],
            "simulated_responses": sum(bool(item["response_status"]) for item in items),
            "simulated_interest_submissions": len(campaign["interest_submissions"]),
            "demo_reviewed_submissions": sum(item["review_status"] == "DEMO_RESEARCH_TEAM_REVIEWED" for item in campaign["interest_submissions"]),
            "simulated_followups_started": sum(item["handoff_status"] == "SIMULATED_FOLLOWUP_STARTED" for item in campaign["interest_submissions"]),
            "real_sends": 0, "real_form_submissions": 0, "formal_consents": 0,
        })
        for item in items:
            submission = submission_by_item.get(item["id"], {})
            status_rows.append({
                "demo_label": "DEMO ONLY — SYNTHETIC EVENTS; NO REAL OUTREACH",
                "campaign_id": campaign["id"], "company_id": item["company_id"], "company_name": item["company_name"],
                "website": item["website"], "qualification_status": item["qualification_status"],
                "identity_status": item["identity_status"], "channel_type": item["channel_type"],
                "recipient_or_form_url": item["recipient"] or item["form_url"], "evidence_url": item["evidence_url"],
                "contact_review_status": item["contact_review_status"], "item_status": item["item_status"],
                "delivery_status": item["delivery_status"], "delivery_attempts": item["delivery_attempts"],
                "simulated_response": item["response_status"], "interest_status": item["interest_status"],
                "form_opened_at": item["form_opened_at"], "token_expires_at": item["token_expires_at"],
                "submission_status": "SIMULATED_SUBMISSION" if submission else "",
                "research_team_review_status": submission.get("review_status", ""),
                "simulated_handoff_status": submission.get("handoff_status", ""),
                "provider": item["provider"], "last_error": item["last_error"], "source_data_mutated": "NO",
            })
    summary_path = output_dir / "phase5d_demo_campaign_summary.csv"
    status_path = output_dir / "phase5d_demo_outreach_status.csv"
    write_csv_atomic(summary_path, SUMMARY_FIELDS, summary_rows)
    write_csv_atomic(status_path, STATUS_FIELDS, status_rows)
    return summary_path, status_path


def seed(store: CampaignStore, data: dict, count: int = 20) -> dict:
    campaign_name = "Professor Walkthrough — DEMO ONLY"
    existing = next((c for c in store.list() if c["name"] == campaign_name), None)
    campaign = store.get(existing["id"]) if existing else store.seed_demo_campaign(data, count=count, name=campaign_name)
    campaign_id = campaign["id"]
    if campaign["status"] == "DRAFT":
        campaign = store.approve_demo(campaign_id)
    audit_events = {item["event_type"] for item in campaign.get("audit", [])}
    if "SIMULATED_DELIVERY_RESULT" not in audit_events and campaign["status"] in {"DEMO_APPROVED", "RUNNING"}:
        pending = sorted((item for item in campaign["items"] if item["item_status"] == "DRAFT_REVIEW"), key=lambda item: item["id"])
        fixture_failures = {item["company_id"]: "FAILED" for item in pending[:2]}
        store.simulate_batch(campaign_id, limit=10, outcomes=fixture_failures)
    # This bounded local replay resumes the persisted queue, with a hard cap of eight batches.
    for _ in range(8):
        campaign = store.get(campaign_id)
        if campaign["status"] not in {"DEMO_APPROVED", "RUNNING"}:
            break
        if not any(item["item_status"] == "DRAFT_REVIEW" and item["delivery_attempts"] < 2 and item["delivery_status"] in {"NOT_SENT", "SIMULATED_FAILED"} for item in campaign["items"]):
            break
        store.simulate_batch(campaign_id, limit=10)
    campaign = store.get(campaign_id)
    delivered = sorted((item for item in campaign["items"] if item["delivery_status"] == "SIMULATED_DELIVERED"), key=lambda item: item["id"])
    response_examples = ("INTERESTED", "DECLINED", "UNANSWERED")
    needed_responses = max(0, 3 - campaign["simulated_response_count"])
    for item, response in zip((item for item in delivered if not item["response_status"]), response_examples[:needed_responses]):
        campaign = store.record_response(campaign_id, item["company_id"], response)
    campaign = store.get(campaign_id)
    existing_submission = campaign["interest_submissions"]
    interested = next((item for item in campaign["items"] if item["delivery_status"] == "SIMULATED_DELIVERED" and item["response_status"] == "SIMULATED_INTERESTED" and item["demo_token"]), None)
    if interested and not interested["form_opened_at"]:
        store.open_demo_form(interested["demo_token"])
    if not existing_submission:
        if interested:
            submission = store.submit_demo_interest(interested["demo_token"], {
                "company_name": interested["company_name"], "website": interested["website"],
                "contact_name": "SIMULATED DEMO RESPONDENT", "contact_role": "Synthetic walkthrough fixture",
                "business_email": "demo-participant@example.invalid", "interest": "INTERESTED",
                "questions": "Synthetic fixture: requests more study information.",
            })
            store.review_demo_submission(int(submission["submission_id"]))
            store.begin_demo_handoff(int(submission["submission_id"]))
    return store.get(campaign_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "export", "reset"))
    parser.add_argument("--database", type=Path, default=DEFAULT_CAMPAIGN_DB)
    parser.add_argument("--company-database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION)
    parser.add_argument("--delivery-events", type=Path, default=DEFAULT_DELIVERY)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--campaign-id", default="", help="Required target for reset; reset removes only one DEMO sidecar campaign")
    args = parser.parse_args()
    store = CampaignStore(args.database)
    if args.action == "reset":
        if not args.campaign_id:
            parser.error("reset requires --campaign-id")
        store.reset_demo_campaign(args.campaign_id)
        print(f"Reset DEMO campaign {args.campaign_id}; company and human-review data were not touched.")
        return
    if args.action == "seed":
        data = load_dashboard_data(args.company_database, args.verification, args.delivery_events)
        campaign = seed(store, data, args.count)
        print(f"campaign={campaign['id']} status={campaign['status']} companies={campaign['company_count']} drafts={campaign['draft_count']} simulated_deliveries={campaign['simulated_deliveries']} simulated_failures={campaign['simulated_failures']} simulated_submissions={len(campaign['interest_submissions'])} real_sends=0 real_form_submissions=0 formal_consents=0")
    paths = export(store, args.output_dir)
    print("exports=" + ",".join(str(path) for path in paths))


if __name__ == "__main__":
    main()
