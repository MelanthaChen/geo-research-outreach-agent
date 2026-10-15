# Phase 6F Website Verification and Contact Discovery Pilot

## Scope and method

Selected 500 OSM records from the Phase 6E Top 5,000 shortlist. Selection uses likely-fit first-pass category, independent-business/domain flags, descending Phase 6E priority, then canonical domain and candidate ID. Only still-unverified OSM records were eligible; previously verified and previously attempted statuses were excluded. The exact selection is recorded in `data/phase6f_selected_500.csv`.

The pilot uses a one-time SQLite backup at `data/phase6f_candidates.db`; Phase 6E shortlist/database, Phase 6D source database, active cohorts, and campaign store are not written. The existing WebsiteCollector, identity verifier, and qualification rules are reused. Only verified + GEO-eligible records are passed to same-domain contact-channel evidence extraction. No guessed contacts, form submissions, outreach approvals, or sends are created.

## Measured results

- Selected: 500 | Processed: 500 | Pending: 0
- Identity verified: 230 (46.0% of processed)
- GEO eligible: 204 (40.8% of processed)
- Companies with first-party contact channels: 134 / 204 GEO-eligible
- Public first-party contact channel records: 260 (business email, contact form, or contact page; all PENDING review)
- HTTP requests: 2332; successful responses: 1615; failed responses: 717; retries: 222
- Runtime: 3085.7s (51.4 min); mean per processed candidate: 6.2s
- Paid-service cost: $0.00 (hard cap; no paid API, LLM, or commercial verification service is used).
- Verification statuses: `{"ACCESS_BLOCKED": 55, "AMBIGUOUS_IDENTITY": 78, "FETCH_FAILED": 93, "INSUFFICIENT_EVIDENCE": 24, "REJECTED_MISMATCH": 14, "ROBOTS_DENIED": 6, "VERIFIED": 230}`
- GEO eligibility statuses: `{"ELIGIBLE": 204, "NEEDS_REVIEW": 296}`
- Stop reason: selection complete or no pending selected candidates

## Remaining work estimate

The Phase 6E Top 5,000 currently has 4,072 unverified OSM records outside completed outcomes (including any selected-but-pending records). At the observed 4.66 HTTP requests and 6.2 seconds per processed candidate, this remaining workload would require about 18,992 requests and 7.0 hours. This is a linear extrapolation from this pilot, not a guarantee.

## Outputs

- `data/phase6f_verified_companies.csv` — verified identity and source/evidence status.
- `data/phase6f_geo_eligible_companies.csv` — verified and eligible under existing rules.
- `data/phase6f_contact_candidates.csv` — public, same-domain evidence; review status remains PENDING.
- `data/phase6f_validation_failures.csv` — processed non-verified outcomes; unprocessed records are not called failures.
- `data/phase6f_selected_500.csv` — deterministic workload selection.

## Interpretation and limitations

A VERIFIED outcome means the existing first-party identity matcher found sufficient name/domain/title/content alignment; it does not certify legal-entity status or independently prove that the business is currently trading. Because source records are OSM points of interest, a match may still identify a physical establishment/location rather than a distinct company; this distinction remains for human review. GEO eligibility is the existing ruleset applied only after identity verification. Current first-party site content, URLs, structured-data types, and qualification signals are retained for review, but website content alone is not an external operating-status check. Contact records are restricted to business-level emails, contact forms, and contact pages; individual names/personal email types are excluded, and all channel review states remain PENDING.

A completed pilot means all 500 have a terminal verification status. Pending records remain explicit if a configured request/time cap stops execution. No contacts are outreach-approved.
