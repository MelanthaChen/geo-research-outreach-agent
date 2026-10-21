#!/usr/bin/env python3
"""Reconcile the saved Phase 7 pool against the SMB/SME target, without crawling."""

from __future__ import annotations

import json

from outreach_agent.phase7_smb import export_reconciliation, write_reconciliation_report


def main() -> None:
    result = export_reconciliation()
    report = write_reconciliation_report(result["summary"])
    summary = result["summary"]
    headlines = {key: summary[key] for key in (
        "phase7_company_records", "unique_candidate_ids", "unique_domains",
        "verified_website_identities", "verified_smb_count", "geo_eligible_smb_count",
        "contactable_smb_count", "remaining_gap_to_5000", "strict_contactable_companies",
        "smb_size_supported_records", "size_unknown_records", "contactable_unknown_size_nonhotel",
        "contactable_identity_review_nonhotel", "large_enterprise_supported_records",
        "conflicting_size_evidence_records",
        "hotel_tagged_candidates_excluded", "hotel_tagged_contactable_excluded",
        "hotel_cross_cohort_domain_overlap",
        "identity_name_review_rows", "contact_channel_breakdown_all_candidates",
        "contact_channel_breakdown_contactable", "contact_channel_breakdown_contactable_smb",
    )}
    print(json.dumps({"summary": headlines, "paths": result["paths"], "report": str(report)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
