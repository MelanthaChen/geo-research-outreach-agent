# Phase 7: Contactable GEO Research Companies

## Strict baseline and current result

- Full existing candidate pool: 57,664 distinct canonical domains; source composition: `{"Greater Dalton Chamber Member Directory": 60, "OpenStreetMap (Geofabrik US Northeast extract)": 57127, "Y Combinator Consumer Startup Directory": 30, "Y Combinator Public Startup Directory": 447}`.
- Strict baseline from the first export before offline evidence recovery: 315 distinct companies.
- Reconciled strict starting baseline after recovery of existing Phase 6 saved contact evidence: 660 distinct companies.
- Phase 7 candidates snapshotted for first-pass processing: 55,911.
- Strict contactable-company count now: 769 (verified identity + existing GEO eligibility + operational first-party website evidence + genuine same-domain business email or confirmed form).
- With public business email: 578; with confirmed form: 279; distinct union: 769.
- Remaining unattempted candidates: 53,232; target shortfall: 4,231.

## Processing and yield

- Unique Phase 7 candidates processed: 2,679; HTTP attempts: 16,869 (successful 12,084, failed 4,793, retries 1,540).
- Union wall time across persisted run intervals: 13984.4s; summed worker runtime (can double-count overlapping workers): 21597.1s.
- Some workers overlapped before a single-worker lock was added; HTTP attempts are actual request-attempt counts, but per-window request/runtime efficiency includes duplicate work during those intervals.
- Website verification outcomes across pool: `{"ACCESS_BLOCKED": 257, "AMBIGUOUS_IDENTITY": 763, "DISCOVERED_UNVERIFIED": 53232, "FETCH_FAILED": 677, "INSUFFICIENT_EVIDENCE": 166, "REJECTED_MISMATCH": 105, "ROBOTS_DENIED": 281, "THIRD_PARTY_URL": 1, "VERIFIED": 2182}`.
- Phase 7 unresolved/failure categories among processed candidates: `{"DNS_FAILURE": 218, "HTTP_400": 1, "HTTP_403_ACCESS_DENIED_UNSPECIFIED": 137, "HTTP_404": 46, "HTTP_409": 2, "HTTP_410": 1, "HTTP_429_RATE_LIMITED": 2, "HTTP_500": 3, "HTTP_502": 1, "HTTP_503": 4, "HTTP_521": 2, "HTTP_525": 2, "HTTP_526": 3, "IDENTITY_AMBIGUOUS": 490, "IDENTITY_MISMATCH_OR_PARKED": 45, "INSUFFICIENT_IDENTITY_EVIDENCE": 99, "INVALID_SOURCE_URL": 1, "OTHER_FETCH_FAILURE": 1, "REDIRECT_ISSUE": 31, "RESPONSE_TOO_LARGE": 67, "ROBOTS_DENIED": 202, "TIMEOUT": 22, "TLS_ERROR": 72, "TRANSIENT_CONNECTION": 3}`.
- Yield metrics are reported in 500-candidate selection-rank windows. Rates use processed records; they are observations, not population guarantees.
- Channel status and outreach authorization are separate; all unreviewed contact channels remain pending and every company export says outreach authorization NOT_GRANTED.

## Evidence, limits, and source scope

The existing Phase 6E pool contains 57,664 distinct domains, including the full 537-company active cohort. No new source was acquired because the existing pool is not exhausted. Website requests use the existing identity verifier, GEO qualification rules, bounded request/time/retry controls, robots/access handling, and saved evidence cache. Blocked, robots-denied, failed, ambiguous, and identity-mismatched sites are not bypassed or promoted. Contact pages without a discovered email or confirmed form do not count.
The ‘operational’ indicator is bounded evidence: a current verified first-party website fetch with a successful 2xx/3xx response and substantive visible text. It does not independently establish business registration or revenue. Companies flagged for possible chain/franchise or repeated-name identity review are excluded from the automated total until resolved.

## Resume

Continue with: `PYTHONPATH=src uv run python scripts/phase7_contacts.py run --batch-size 500 --request-cap 21869 --time-cap-seconds 24784 --chunk-request-budget 500`.
The request and time caps are cumulative across saved Phase 7 runs; saved wall intervals are unioned to avoid double-counting overlap. Raise the caps to allow additional work. No email or form submissions are performed.

## Outputs

- `data/phase7_contactable_companies.csv`
- `data/phase7_contact_channel_audit.csv`
- `data/phase7_validation_failures.csv`
- `data/phase7_exclusion_queue.csv`
- `data/phase7_yield_metrics.csv`
- `data/phase7_candidates.db`
