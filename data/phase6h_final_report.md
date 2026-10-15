# Phase 6H: Scale Verification and Contact Discovery

## Reconciled baseline

- Top 5,000 shortlist rows / unique IDs: 5000 / 5000
- Phase 6E outcomes already terminal: 428
- Phase 6F candidates already selected: 500
- Exact remaining first-pass workload at start: 4072
- Candidate input is the Phase 6E OSM shortlist; no companies were acquired.

## Phase 6H execution results

- Newly processed by Phase 6H: 671 / 4,072
- Total processed in Top 5,000 shortlist: 1,599 / 5,000
- Remaining first-pass work after this run: 3,401
- Verified identities across shortlist: 907
- GEO eligible under existing rules across shortlist: 855
- Companies with public business email evidence: 120
- Companies with confirmed first-party contact form evidence: 105
- Contact-page-only companies: 118
- Unique companies with any first-party channel evidence: 308
- Unique companies with usable email or confirmed form: 190
- New unique companies with public business email: 70
- New unique companies with confirmed official contact forms: 63
- New contact-page-only companies: 64
- New unique companies with usable channels (email/form): 110
- New channel records pending human review: 288 (total shortlist pending: 514)
- HTTP attempts: 3,000 (2,220 successful responses; 780 failed; 209 retries)
- Runtime: 3714.3s across persisted batches; invocation runtime is reported separately by CLI.
- Phase 6H status distribution: `{"ACCESS_BLOCKED": 34, "AMBIGUOUS_IDENTITY": 142, "DISCOVERED_UNVERIFIED": 3401, "FETCH_FAILED": 111, "INSUFFICIENT_EVIDENCE": 34, "REJECTED_MISMATCH": 13, "ROBOTS_DENIED": 69, "VERIFIED": 268}`
- Full Top 5,000 status distribution: `{"ACCESS_BLOCKED": 96, "AMBIGUOUS_IDENTITY": 226, "DISCOVERED_UNVERIFIED": 3401, "FETCH_FAILED": 207, "INSUFFICIENT_EVIDENCE": 60, "REJECTED_MISMATCH": 27, "ROBOTS_DENIED": 76, "VERIFIED": 907}`

## Batch quality checks

- Automatic pause floor: a completed batch pauses further requests if identity verification is below 23.0% or GEO eligibility below 20.4% (half of observed Phase 6F rates, 46.0% and 40.8%).
- 250-candidate cohort completion and yield rates: `[{"batch_number": 1, "geo_eligibility_rate": 0.368, "identity_verification_rate": 0.4, "processed": 250, "rank_end": 250, "rank_start": 1, "selected": 250, "state": "COMPLETE"}, {"batch_number": 2, "geo_eligibility_rate": 0.352, "identity_verification_rate": 0.388, "processed": 250, "rank_end": 500, "rank_start": 251, "selected": 250, "state": "COMPLETE"}, {"batch_number": 3, "geo_eligibility_rate": 0.3801, "identity_verification_rate": 0.4152, "processed": 171, "rank_end": 750, "rank_start": 501, "selected": 250, "state": "PARTIAL"}]`
- Exact HTTP attempts, successes/failures, retries, and runtime are recorded per checkpoint chunk in `phase6h_execution_metrics.csv`.

## Data handling and controls

Phase 6H uses a one-time SQLite backup of the Phase 6F candidate database. The frozen Phase 6E/6F stores are read-only inputs and are not written. Every candidate checkpoint is committed by the existing verifier; each batch's requests, timing, counts, and stop reason are persisted in the Phase 6H run ledger. New contact evidence uses the inherited candidate contact table and stays PENDING in the existing Phase 6G review sidecar. Website fetching respects robots controls, rate limits, response/time/request caps, and does not bypass bot protection. No emails or forms were sent/submitted.

## Output files

- `data/phase6h_verified_companies.csv`
- `data/phase6h_geo_eligible_companies.csv`
- `data/phase6h_business_email_contacts.csv`
- `data/phase6h_official_contact_forms.csv`
- `data/phase6h_contact_review_queue.csv`
- `data/phase6h_execution_metrics.csv`

A full first-pass completion means every selected record has a terminal saved verification status. If a request/time budget, site access restriction, or the quality pause is reached, pending records remain explicit for resumption. GEO eligibility and channel evidence are not human approvals or outreach authorization.
