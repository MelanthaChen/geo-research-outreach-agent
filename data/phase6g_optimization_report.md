# Phase 6G Contact Quality Audit and Pipeline Optimization

## Scope and integrity

The audit reads the saved Phase 6F selection and contact evidence. It does not edit `phase6f_candidates.db`, the 500-candidate Phase 6F exports, source candidate databases, company cohorts, or campaign history. Human contact/company review decisions are isolated in `data/phase6g_contact_review.db`. No email was sent and no form was submitted.

- Phase 6F companies with saved channels: **134**
- Raw Phase 6F channel records inspected: **260** (semantic contact duplicates collapsed to **226** audit rows; repeated records represented by duplicate counts: **34**).
- Distinct companies with public business email evidence: **50**.
- Distinct companies with a confirmed official contact-form structure: **42**.
- Distinct companies with contact-page evidence only (no confirmed form/email): **54**.
- Explicit named individual channels retained in Phase 6F: **0**; none were present because Phase 6F excludes person-specific contact details.
- Unsupported/ambiguous channels: **0**; channels pending human review: **226**.
- Usable channel-candidate share (email or confirmed form; not human approved): **59.7%** of channel-bearing companies.

## Channel classification and confidence

| Classification | Deduplicated records |
|---|---:|
| OFFICIAL_CONTACT_FORM | 49 |
| OFFICIAL_CONTACT_PAGE_NO_CONFIRMED_METHOD | 125 |
| PUBLIC_BUSINESS_EMAIL | 52 |

Confidence is deterministic and transparent, based on same-domain provenance, verified company identity, captured purpose/evidence, and whether a usable submission method is actually evidenced. It is not an approval. Contact-page-only rows are downgraded because the method is unconfirmed. All remaining channels retain PENDING review until a person records a decision.

## Phase 6F verification outcome breakdown

The selected cohort had 500 companies: 93 FETCH_FAILED, 55 ACCESS_BLOCKED, 6 ROBOTS_DENIED, 78 AMBIGUOUS_IDENTITY, 24 INSUFFICIENT_EVIDENCE, 14 REJECTED_MISMATCH, and 230 VERIFIED. The CSV carries one row per non-verified company/status for traceability.

| Failure / unresolved category | Records |
|---|---:|
| DNS_FAILURE | 44 |
| HTTP_403_ACCESS_DENIED_UNSPECIFIED | 55 |
| HTTP_404 | 7 |
| HTTP_500 | 1 |
| HTTP_525 | 2 |
| HTTP_526 | 1 |
| IDENTITY_AMBIGUOUS | 78 |
| IDENTITY_MISMATCH_OR_PARKED | 7 |
| INSUFFICIENT_IDENTITY_EVIDENCE | 24 |
| INVALID_DOMAIN | 1 |
| INVALID_SOURCE_URL | 1 |
| NON_HTML_CONTENT | 1 |
| REDIRECT_ISSUE | 7 |
| RESPONSE_TOO_LARGE | 13 |
| ROBOTS_DENIED | 6 |
| TIMEOUT | 7 |
| TLS_ERROR | 14 |
| TRANSIENT_CONNECTION | 1 |

Important: the 55 access-blocked cases are HTTP 403 responses. The stored evidence does not establish whether each is an explicit policy denial or bot protection; no CAPTCHA/bot-protection marker was captured in the saved evidence. Robots denials remain separately identified and were not retried. HTTP 429s, if present, are separately rate-limit categorized. Redirected unrelated sites stay identity mismatches, not fetch failures.

## Safe pipeline changes and measured effects

- Contact routes are deduplicated in the audit layer using company + channel kind + normalized email/route. The 260 source rows collapse to the row count above; original evidence remains untouched and duplicate counts are retained.
- Existing per-company page deduplication (`seen` URL set), persistent Phase 6F candidate checkpoints, bounded request budgets, same-client connection reuse within a batch, and idempotent channel inserts are retained. Phase 6G review writes go only to a separate database.
- A deterministic response-size failure no longer consumes a retry: retry handling now retries transport `RequestError`s but not deterministic response-cap `ValueError`s. A regression test verifies one request rather than retrying the same over-limit response. Phase 6F has 13 recorded response-too-large cases; because historical retries are not attributed per company/error, historical request savings cannot be claimed.
- A 401/403/429 response from `robots.txt` now fails closed as ROBOTS_DENIED before sitemap or company-page requests. The exact status and reason are preserved; neither site policy nor anti-bot controls are bypassed.
- No full recrawl was run to measure caching. Existing checkpoints prevent already-terminal companies from being fetched again on resume; Phase 6G's comparison sample is isolated in a Phase 6G database copy.

### Bounded comparison sample

Re-evaluated **30 / 30** deterministically selected prior cases (30 total selected): DNS/TLS/timeout/connection failures and ambiguous identities only. Request cap: 180; time cap: 1200 seconds; per-request timeout: 12.0 seconds; batch size: 10; no access-blocked or robots-denied site was selected. Actual requests: 168 (66 succeeded, 102 failed); retries: 48; summed collector runtime: 302.46 seconds.
Initial sample statuses: `{'FETCH_FAILED': 15, 'AMBIGUOUS_IDENTITY': 15}`. Current statuses: `{'AMBIGUOUS_IDENTITY': 15, 'FETCH_FAILED': 15}`. Verified after re-check: **0**; GEO eligible after re-check: **0**.
This is a bounded before/after observation, not a controlled proof of higher precision or population-wide yield. Ambiguous identity is only promoted when the existing first-party identity verifier supports it. Any remaining sample items are checkpointed and can be resumed without repeating terminal cases.

## Human review workflow and remaining blockers

The Companies page now includes a paginated batch review queue with source/confidence/review filters, company identity and GEO context, source and contact evidence links, reviewer-only approve/reject decisions for individual evidence channels, a company `Further review` state, and a filtered CSV export. The review sidecar cannot set campaign approval, research authorization, or outreach-ready state. Non-first-party and unverified identity evidence cannot be marked APPROVED through the backend.

Contact channel approval remains separate from research participation consent and campaign-send approval. A contact page alone is not a usable form/email. Email and form evidence still requires human suitability review, freshness review, and any applicable research/ethics authorization before any campaign preparation. No sending is enabled by this phase.

### Generated files

- `data/phase6g_contact_quality_audit.csv` — deduplicated channel audit with quality factors and source evidence.
- `data/phase6g_contact_review_queue.csv` — same deterministic records for dashboard/export review.
- `data/phase6g_verification_failure_analysis.csv` — selected Phase 6F failures, unresolved identity cases, evidence, and safe next action.
- `data/phase6g_contact_review.db` — local human decisions, separate from source data.
- `data/phase6g_evaluation.db` — isolated checkpoint copy for the bounded comparison pilot.
