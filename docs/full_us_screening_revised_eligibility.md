# Full US screening revised eligibility

Run date: 2026-10-22  
Run ID: `full-screening-aIdr0NtN__ISCA`  
Run version: `full-us-outreach-screening/v2-soft-size`

## Correction applied

Company size and employee-count evidence are now soft prioritization fields. Missing size evidence no longer blocks preliminary GEO screening. Existing size metadata remains in the registry and in the export as `size_status` and `size_preference`.

The mandatory gates remain:

- `VERIFIED_US` geography.
- Verified business identity or verified physical hotel property.
- Verified official website.
- Existing `ELIGIBLE` qualification.
- Valid public business email with preserved source URL and evidence.
- No suppression or exclusion.

The owner-controlled GeoAIResume fixture remains excluded from formal counts.

## Population counts

| Requirement | Companies | Hotels |
|---|---:|---:|
| Existing candidates | 57,664 | 15,262 |
| US geographic classification | 57,574 | 15,262 |
| Identity verified within US | 20,803 | 912 |
| Official website verified within US | 20,803 | 912 |
| Qualified with verified website | 18,942 | 879 |
| Contact evidence present within US | 13,091 | 465 |
| Valid public business email on qualified records | 5,077 | 285 |
| Revised eligible for preliminary GEO screening | 5,077 | 285 |

The revised deterministic workload is therefore 5,362 records: 5,077 companies and 285 hotels. Contact pages and forms were not counted as email eligibility.

## Execution result

All 72,927 registry records were reprocessed into the existing durable queue. The queue is resumable and idempotent. No source candidate database was reset, rewritten, or destructively migrated.

| Result | Count |
|---|---:|
| Total queued for AI GEO screening | 5,362 |
| Completed AI screening | 0 |
| Blocked at AI provider boundary | 5,362 |
| Final outreach-ready records | 0 |
| Paid calls | 0 |
| Recorded screening cost | $0.00 |

No approved local/free AI provider is configured. The run paused before screening rather than substituting mock output. The exact remaining workload is 5,362 lightweight preliminary website evaluations. A defensible paid cost estimate cannot be produced without an approved provider, model, prompt/token budget, and pricing configuration; inventing one would be misleading.

## Remaining blockers

The deterministic queue retains explicit blockers for every non-eligible record. The main remaining blockers are:

- Geographic status not `VERIFIED_US`.
- Identity not verified.
- Qualification not `ELIGIBLE`.
- Missing official website.
- No valid public business email.
- Suppression or exclusion.
- Owner-controlled test exclusion.
- For the eligible population only: no approved AI screening provider.

AI screening must evaluate actual website evidence for entity clarity, product/service discoverability, realistic customer questions, AI-readable factual content, geographic relevance, citation-worthy information, structured business information, and plausible content gaps. It must assign `HIGH_GEO_POTENTIAL`, `MEDIUM_GEO_POTENTIAL`, `LOW_GEO_POTENTIAL`, or `INSUFFICIENT_EVIDENCE` with supporting URLs and excerpts. No current absence from AI search, uplift percentage, ranking, or improvement claim is asserted here.

`READY_FOR_EMAIL` remains unavailable until a real screening produces HIGH or MEDIUM evidence-backed GEO potential and the existing consent/recruitment gates are satisfied.

## Outputs and tests

- Complete CSV: [`data/full_screening/full_us_outreach_screening.csv`](/Users/melc/Documents/Intern/LLM%20Search%20Optimizer/geo-research-outreach-agent/data/full_screening/full_us_outreach_screening.csv)
- Complete XLSX: [`data/full_screening/full_us_outreach_screening.xlsx`](/Users/melc/Documents/Intern/LLM%20Search%20Optimizer/geo-research-outreach-agent/data/full_screening/full_us_outreach_screening.xlsx)
- Durable state: `data/research_registry.db`, `full_screening_jobs` and `full_screening_queue`

Tests run:

```text
PYTHONPATH=src uv run pytest -q tests/test_full_screening.py
2 passed

PYTHONPATH=src uv run pytest -q
full suite passed, exit code 0
```

No email was sent, no external form was submitted, no paid AI call or deployment occurred, and no GEO Engine deep experiment was launched. The hotel worker was not interrupted.
