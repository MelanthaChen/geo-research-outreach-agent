# Phase 7: Contactable GEO Research Companies

> Target correction: the 5,000-company goal means SMB/SME businesses, not only venture-backed startups. The all-industry strict contactable total below is not the SMB target count. Run `PYTHONPATH=src uv run python scripts/phase7_smb_reconcile.py` for the offline size-evidence-based reconciliation and separate hotel exclusion. Current report: [docs/phase7_smb_target_reconciliation.md](../docs/phase7_smb_target_reconciliation.md).

## Strict baseline and current result

- Full existing candidate pool: 57,664 distinct canonical domains; source composition: `{"Greater Dalton Chamber Member Directory": 60, "OpenStreetMap (Geofabrik US Northeast extract)": 57127, "Y Combinator Consumer Startup Directory": 30, "Y Combinator Public Startup Directory": 447}`.
- Strict baseline from the first export before offline evidence recovery: 315 distinct companies.
- Reconciled strict starting baseline after recovery of existing Phase 6 saved contact evidence: 660 distinct companies.
- Phase 7 candidates snapshotted for first-pass processing: 55,911.
- Strict contactable-company count now: 7,172 (verified identity + existing GEO eligibility + operational first-party website evidence + genuine same-domain business email or confirmed form).
- With public business email: 5,040; with confirmed form: 2,988; distinct union: 7,172.
- Remaining unattempted candidates: 10,407; Phase 7 continues to pool completion and does not stop at 5,000 contactable companies.

## Processing and yield

- Unique Phase 7 candidates processed: 45,504; HTTP attempts: 149,951 (successful 113,405, failed 36,556, retries 11,302).
- Union wall time across persisted run intervals: 83523.7s; summed worker runtime (can double-count overlapping workers): 85084.6s.
- Some workers overlapped before a single-worker lock was added; HTTP attempts are actual request-attempt counts, but per-window request/runtime efficiency includes duplicate work during those intervals.
- Website verification outcomes across pool: `{"ACCESS_BLOCKED": 2485, "AMBIGUOUS_IDENTITY": 9062, "DISCOVERED_UNVERIFIED": 10407, "FETCH_FAILED": 8043, "INSUFFICIENT_EVIDENCE": 2019, "REJECTED_MISMATCH": 1079, "ROBOTS_DENIED": 3689, "THIRD_PARTY_URL": 1, "VERIFIED": 20879}`.
- Phase 7 unresolved/failure categories among processed candidates: `{"DNS_FAILURE": 3439, "HTTP_400": 22, "HTTP_402": 6, "HTTP_403_ACCESS_DENIED_UNSPECIFIED": 2353, "HTTP_404": 1196, "HTTP_405": 2, "HTTP_409": 19, "HTTP_410": 13, "HTTP_418": 1, "HTTP_421": 2, "HTTP_423": 1, "HTTP_429_RATE_LIMITED": 14, "HTTP_444": 4, "HTTP_451": 2, "HTTP_453": 1, "HTTP_456": 1, "HTTP_500": 64, "HTTP_502": 14, "HTTP_503": 36, "HTTP_504": 1, "HTTP_509": 2, "HTTP_511": 1, "HTTP_520": 5, "HTTP_521": 22, "HTTP_522": 1, "HTTP_523": 9, "HTTP_525": 16, "HTTP_526": 22, "HTTP_530": 5, "IDENTITY_AMBIGUOUS": 8789, "IDENTITY_MISMATCH_OR_PARKED": 486, "INSUFFICIENT_IDENTITY_EVIDENCE": 1952, "INVALID_DOMAIN": 12, "INVALID_SOURCE_URL": 44, "NON_HTML_CONTENT": 6, "OTHER_FETCH_FAILURE": 26, "REDIRECT_ISSUE": 564, "RESPONSE_TOO_LARGE": 1199, "ROBOTS_DENIED": 3610, "TIMEOUT": 454, "TLS_ERROR": 1077, "TRANSIENT_CONNECTION": 90}`.
- Yield metrics are reported in 500-candidate selection-rank windows. Rates use processed records; they are observations, not population guarantees.
- Channel status and outreach authorization are separate; all unreviewed contact channels remain pending and every company export says outreach authorization NOT_GRANTED.

## Evidence, limits, and source scope

The existing Phase 6E pool contains 57,664 distinct domains, including the full 537-company active cohort. No new source was acquired because the existing pool is not exhausted. Website requests use the existing identity verifier, GEO qualification rules, bounded request/time/retry controls, robots/access handling, and saved evidence cache. Blocked, robots-denied, failed, ambiguous, and identity-mismatched sites are not bypassed or promoted. Contact pages without a discovered email or confirmed form do not count.
The ‘operational’ indicator is bounded evidence: a current verified first-party website fetch with a successful 2xx/3xx response and substantive visible text. It does not independently establish business registration or revenue. Companies flagged for possible chain/franchise or repeated-name identity review are excluded from the automated total until resolved.

## Resume

Continue with: `PYTHONPATH=src uv run python scripts/phase7_contacts.py run --batch-size 500 --request-cap 154951 --time-cap-seconds 94323 --chunk-request-budget 500 --concurrency 4`.
The request and time caps are cumulative across saved Phase 7 runs; saved wall intervals are unioned to avoid double-counting overlap. Raise the caps to allow additional work. No email or form submissions are performed.

## Outputs

- `data/all_processed_candidates.csv`
- `data/all_verified_companies.csv`
- `data/all_geo_eligible_companies.csv`
- `data/all_contactable_companies.csv`
- `data/business_email_contacts.csv`
- `data/official_contact_forms.csv`
- `data/pending_human_review.csv`
- `data/full_pool_execution_report.md`
- `data/phase7_contactable_companies.csv`
- `data/phase7_contact_channel_audit.csv`
- `data/phase7_validation_failures.csv`
- `data/phase7_exclusion_queue.csv`
- `data/phase7_yield_metrics.csv`
- `data/phase7_candidates.db`
