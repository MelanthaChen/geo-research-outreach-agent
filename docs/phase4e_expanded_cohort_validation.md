# Phase 4E: Expanded Cohort Website Verification & GEO Qualification

## Outcome

The 121 Phase 4D additions were inspected in the isolated `data/phase4d_experimental.db`; the frozen 77-company source database was not changed. The existing bounded crawler captured or reused evidence for 120 company domains, while one Chamber URL was a LinkedIn profile and was rejected before requesting it. First-party identity checks conservatively verified 97 websites. The existing Phase 3D qualification rules were then run only for those 97 verified identities: 76 became `ELIGIBLE`, while 21 remain `NEEDS_REVIEW`. None was `INELIGIBLE`. All 121 companies still have human review state `PENDING`.

Verification is not qualification: an identity can be verified even where content is thin or GEO opportunity is unknown. Conversely, a successful HTTP response alone is never sufficient for identity verification. The original company IDs, provenance rows, and human-review decisions were preserved.

## Method

`scripts/phase4e_verify_expansion.py` selects additions by comparing normalized domains against the frozen Phase 3D 77-company review export; it asserts exactly 121 selected records. It writes evidence back only to the Phase 4D experimental database. Source names, source identifiers and source profile URLs are included in the row-level audit. Historical discovery rows do not contain a source directory route for these imports, so that field is blank rather than reconstructed.

The script uses the existing `WebsiteCollector` and cache, sequentially, in batches of at most 25. Its effective per-company page cap is two, request timeout is 15 seconds, request delay is one second, retry count is one retry after the initial attempt, response cap is 2 MB, and cache TTL is 168 hours. The existing crawler checks robots rules, respects denial, and records failures/blocks without bypass. A discovered third-party social URL is not fetched. The identity checker labels each result `VERIFIED`, `AMBIGUOUS_IDENTITY`, `INSUFFICIENT_EVIDENCE`, `FETCH_FAILED`, `ACCESS_BLOCKED`, `ROBOTS_DENIED`, `THIRD_PARTY_URL`, or `REJECTED_MISMATCH`; an inaccessible site is not called a mismatch.

The site identity test uses saved final URL, title, metadata and visible text plus normalized company/domain tokens. Explicit parking or unrelated repurposed content can be rejected; a cross-domain redirect is rejected only when no company identity evidence remains. Ambiguous evidence is not promoted. `usable_website_content` is separately recorded and means at least 500 visible characters on a successfully captured page; it does not establish identity.

The existing qualification function/configuration was reused without new scoring or thresholds, and only for `VERIFIED` identities. It keeps eligibility, GEO opportunity, research priority, confidence and human review as distinct fields. Source-reported YC team size is retained as such; missing size is not inferred. Contact availability is not a qualification input. All qualification rows include contact status to make that separation auditable.

## Website verification results

| Identity status | YC (111) | Chamber (10) | Total |
| --- | ---: | ---: | ---: |
| `VERIFIED` | 96 | 1 | 97 |
| `AMBIGUOUS_IDENTITY` | 7 | 5 | 12 |
| `ACCESS_BLOCKED` | 4 | 1 | 5 |
| `FETCH_FAILED` | 2 | 2 | 4 |
| `ROBOTS_DENIED` | 1 | 0 | 1 |
| `INSUFFICIENT_EVIDENCE` | 1 | 0 | 1 |
| `THIRD_PARTY_URL` | 0 | 1 | 1 |
| `REJECTED_MISMATCH` | 0 | 0 | 0 |

Overall identity-verification rate is 97/121 (80.2%). Access-block rate is 5/121 (4.1%); fetch failures are 4/121 (3.3%), with robots denial separately reported as 1/121 (0.8%). Twelve records (9.9%) have ambiguous identity. No record had sufficient explicit mismatch evidence for `REJECTED_MISMATCH`; this is not evidence that no wrong source associations exist, only that no saved response justified that conclusion. `INSUFFICIENT_EVIDENCE` is one (0.8%).

The pages contained at least 500 visible characters for 88 companies: 76 verified identities and 12 ambiguous identities. Twenty-one verified sites were reachable but did not meet that content-length indicator. “Usable content” is therefore not interchangeable with “verified website.” The single third-party URL is the Greater Dalton Chamber listing for “Chattanooga Area Food Bank Northwest Georgia Area Food Bank,” whose supplied URL is `https://linkedin.com/in`; it was not fetched. No blocked or failed record was reclassified as an incorrect identity.

YC supplied 96 verified identities out of 111 (86.5%); Chamber supplied 1 of 10 (10%). Chamber also had five ambiguous identities, two fetch failures, one blocked site, and the LinkedIn URL. This source split is descriptive of this bounded cohort, not a general source-quality estimate.

## Existing qualification results

The qualification function ran on the 97 verified records only. All other additions remain at their Phase 4D `NEEDS_REVIEW` / `UNKNOWN` state. `ELIGIBLE` here means the existing rules found an active discovery record and sufficiently analyzable website evidence; it is not proof of SMB status or approval for outreach.

| Output | YC (111) | Chamber (10) | Total (121) |
| --- | ---: | ---: | ---: |
| Eligibility `ELIGIBLE` | 75 | 1 | 76 |
| Eligibility `NEEDS_REVIEW` | 36 | 9 | 45 |
| Eligibility `INELIGIBLE` | 0 | 0 | 0 |
| GEO `HIGH` | 49 | 1 | 50 |
| GEO `MEDIUM` | 17 | 0 | 17 |
| GEO `LOW` | 9 | 0 | 9 |
| GEO `UNKNOWN` | 36 | 9 | 45 |
| Research priority `HIGH` | 49 | 1 | 50 |
| Research priority `MEDIUM` | 17 | 0 | 17 |
| Research priority `LOW` | 9 | 0 | 9 |
| Research priority `NEEDS_REVIEW` | 36 | 9 | 45 |

The 12 missing employee-size values are visible: 2 YC and all 10 Chamber companies. The other 109 values are reported by the YC discovery directory and are not independently verified. Chamber membership was not treated as size evidence. Existing qualification also does not infer missing size or change thresholds. All 121 company records require human review because their review status remains `PENDING`, irrespective of machine eligibility or priority.

The qualification audit contains an intentionally important limitation: the existing qualification engine consumes the crawler's existing extracted signals, not the new conservative identity decision as a standalone signal. The workflow gates qualification to identity-verified IDs, but the existing rule engine can still mark a verified company `ELIGIBLE` even when the separate content-length indicator is false. Reviewers should inspect its saved page evidence and the row-level outcomes rather than interpreting `ELIGIBLE` as an identity verdict.

## Bounded contact-channel sample

Only after qualification, the workflow selected the ten highest-scoring verified additions at `HIGH` research priority (company IDs 83, 88, 91, 99, 101, 102, 105, 108, 110, 111). Contact parsing consumed already saved website snapshots. Crawl4AI fallback was disabled, no further website requests were made for contact extraction, and no messages or forms were sent. Four companies had a contact-channel result and six did not; seven contact records were created in the first run. On the idempotency rerun, zero additional contacts were created and six existing records were recognized as duplicates. This small sample is not a cohort-wide coverage estimate. Contact coverage is not used to rank research priority.

## Resume, repeatability and preservation

An early live run was interrupted by a deterministic JSON-LD parsing error after the first three 25-record batches had committed snapshots. The parser was corrected to handle scalar and array `@type` values. Resuming reused the successful cached snapshots for those batches and processed only the remaining uncached companies; no successful snapshot was overwritten by a failed fetch. A subsequent full workflow run reported all 120 fetchable domains as cached, zero fetched, zero failed and zero blocked. The one social URL remained excluded.

The expanded database remains at 198 companies, 201 provenance rows and 198 distinct normalized domains. The 121 additions retain 121 pending human-review states. Repeated workflow and review exports were byte-stable: SHA-256 values were identical across runs for the website verification, qualification and expanded review CSVs. The frozen `data/phase3d_company_review_77.csv` SHA-256 remained `b9c03fe99828b1cfd58501f764665e0a80f009c20f2d234e6e345b08e9364f2f`; the frozen database artifact is not present in this checkout, so its earlier recorded hash cannot be recomputed here. No canonical 77-company database was opened as the workflow target.

Tests: full suite **113 passed**; `git diff --check` clean. The verification workflow and export are scoped to the experimental database and the 121 non-baseline domains.

## Reproduction

```sh
OUTREACH_DATABASE_URL=sqlite:///data/phase4d_experimental.db PYTHONPATH=src uv run python scripts/phase4e_verify_expansion.py --batch-size 25 --contact-sample-limit 10
OUTREACH_DATABASE_URL=sqlite:///data/phase4d_experimental.db PYTHONPATH=src uv run python -m outreach_agent export-review --output data/phase4e_expanded_company_review.csv
```

Add `--no-contact-sample` to run website verification and qualification without the bounded contact sample. Fresh snapshots are reused automatically; this flag does not refresh cached websites. The exports are `data/phase4e_website_verification.csv`, `data/phase4e_qualification_results.csv`, and `data/phase4e_expanded_company_review.csv`.

## Artifacts and limitations

- `data/phase4e_website_verification.csv`: source provenance, submitted and normalized URL/domain, status/reason, redirect/fetch information, page title, evidence tokens, content indicator, and size evidence.
- `data/phase4e_qualification_results.csv`: separate identity, eligibility, GEO opportunity, priority, confidence, size, review, and contact fields.
- `data/phase4e_expanded_company_review.csv`: existing unified review export across all 198 experimental companies.

This is a bounded evidence pass, not independent manual confirmation of 97 businesses. Automated token evidence can miss brands, DBAs and parent-company relationships; ambiguous cases need human verification. The Chamber sample is only ten records and has low automated identity-verification yield. No production database was expanded, no cohort-wide contact enrichment was run, and no outreach was sent.
