# Full US outreach screening run

Run date: 2026-10-22  
Run ID: `full-screening-7Dq-BI0GHVwVZA`  
Run version: `full-us-outreach-screening/v1`

## Outcome

The complete deterministic pass processed all 72,927 normal registry records:

| Cohort | Registry records | Deterministic AI-screening candidates | AI-screened | Final ready for email |
|---|---:|---:|---:|---:|
| Companies | 57,664 | 0 | 0 | 0 |
| Hotels | 15,262 | 285 | 0 | 0 |
| Owner-controlled test | 1 | 0 | 0 | 0 |

The run is `PAUSED_AT_AI_BOUNDARY`. No paid provider was configured, so the 285 hotel records were retained as `BLOCKED_AI_PROVIDER`. No mock AI output was used and no record was marked `READY_FOR_EMAIL`.

## Deterministic gates

The worker used the existing lifecycle registry and preserved source IDs, cohort, geographic classification, identity state, qualification state, website, and contact evidence. It did not modify either source candidate database.

The gates were:

- `VERIFIED_US` geographic classification.
- Verified identity and an official website.
- Existing `ELIGIBLE` qualification.
- For companies, explicit SMB size evidence. The current company registry has `SIZE_UNKNOWN` for these source records, so all 57,664 companies are blocked with `SMB_SIZE_EVIDENCE_MISSING`. An OSM local-business tag is not treated as SMB proof.
- A syntactically valid public business email. Contact pages and forms do not satisfy this gate.
- No consent or recruitment suppression.
- Owner-controlled test records excluded from formal cohorts.

Current supporting counts in the registry are 18,942 US-verified, identity-verified, qualified companies and 879 hotels. Of those, 13,091 companies and 465 hotels have contact evidence. The strict run narrows the actionable pre-AI set to 285 hotels with valid public business email evidence.

## AI boundary and accounting

Provider: `UNCONFIGURED`  
Provider mode: `NO_PAID_CALLS`  
AI successes: 0  
AI failures: 0  
AI-blocked workload: 285 hotel websites  
Input tokens: 0  
Output tokens: 0  
Recorded cost: `$0.00`

There is no configured provider/model pricing or approved budget in this repository, so a dollar estimate would be fabricated. The exact paid-boundary workload is 285 preliminary screenings. The worker can resume idempotently after an approved local/free provider adapter is installed; a paid provider must not be enabled by environment variable alone without explicit approval and a pricing configuration.

Existing observed website evidence was not relabeled as a new AI screening result. The content-cache table is empty for this run because the existing verification evidence is not a validated full-screening page-result envelope. This preserves the distinction between historical verification evidence and a new preliminary GEO screen.

## Outputs

- Complete CSV: [`data/full_screening/full_us_outreach_screening.csv`](/Users/melc/Documents/Intern/LLM%20Search%20Optimizer/geo-research-outreach-agent/data/full_screening/full_us_outreach_screening.csv)
- Complete XLSX: [`data/full_screening/full_us_outreach_screening.xlsx`](/Users/melc/Documents/Intern/LLM%20Search%20Optimizer/geo-research-outreach-agent/data/full_screening/full_us_outreach_screening.xlsx)
- Durable job and queue state: `data/research_registry.db`, tables `full_screening_jobs` and `full_screening_queue`

The export contains one row per registry record and explicit blocker/status fields. `opportunity_level` is `UNAVAILABLE` until a real approved screening provider produces evidence.

## Tests and safety

Focused full-run tests passed:

```text
PYTHONPATH=src uv run pytest -q tests/test_full_screening.py
2 passed
```

The existing full suite had previously passed with 279 tests before this worker was added. The new worker itself performed zero paid calls, zero email sends, zero form submissions, zero deployments, and no GEO Engine audit or experiment launch. The independent hotel worker was not stopped, restarted, reconfigured, or given access to this screening state.

## Unresolved dependencies

1. Company SMB evidence is absent from the current source/registry projection. A future non-destructive enrichment must provide auditable employee, revenue, ownership, or equivalent SMB evidence before companies can enter AI screening or outreach.
2. No approved local/free AI screening provider is configured. A provider adapter must preserve model, prompt, token, cost, failure, and evidence provenance before the 285 hotel queue can resume.
3. AI opportunity levels, evidence-backed findings, and final outreach readiness remain unavailable. The report intentionally does not invent them.
