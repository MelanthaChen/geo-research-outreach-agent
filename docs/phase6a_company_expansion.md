# Phase 6A company expansion: permission-gated, currently paused

Assessment date: 2026-10-09.

## Outcome

Phase 6A preserved the existing 537-company Phase 4F cohort as an isolated copy at `data/phase6a_experimental.db`. It did **not** reach 5,000: it contains 537 companies and 540 discovery-provenance rows, leaving a gap of 4,463. Zero new candidates were discovered or accepted, because no additional direct-ingestion source cleared both permission and research-fit review. No live website crawling or contact discovery was run. No companies, websites, contacts, qualification evidence, or human decisions were fabricated.

The input Phase 4F database remains byte-identical at SHA-256 `53ff6af9b525e73e195d2c251e0d06ebfc17efa5189f35ef5c9e8314e7bca3c2`; the frozen 77-company database remains byte-identical at `b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6`. Campaign sidecar hash remains `7de8928158741ccc66928123d64ded56f6a66e1eccd71301ad2226745c959203`. A row-level copy validation confirmed matching baseline table counts and SQLite integrity. Phase 5D campaign records were not modified.

The current dashboard default now points to the Phase 6A copy, not silently to the old Phase 4F database. It remains read-only for company data, preserves existing company IDs and details, and continues to use the isolated campaign sidecar. Existing API pagination remains capped at 100 company list rows per page. The dashboard still builds the full 537-record in-memory read model on startup; this does not yet meet the “do not load thousands of full evidence records” requirement for a future 5,000-company live cohort. A SQL-backed paginated repository and browser-memory selection across all filtered pages are required before claiming scale readiness.

An offline local smoke benchmark on the 537-record snapshot took 0.065 seconds to build the dashboard read model and measured 20 sequential 50-row API page requests at a 0.86 ms median / 1.01 ms p95. This is a local single-process smoke measurement, not a concurrent-user or 5,000-row projection. Its response remains bounded to 50 full company list summaries; detail is requested per selected company. Startup materialization is the scale bottleneck, so no 1,000/2,500/5,000 runtime projection is made.

## Measured baseline

The offline report is [data/phase6a_expansion_progress.csv](../data/phase6a_expansion_progress.csv), source yields are [data/phase6a_source_yields.csv](../data/phase6a_source_yields.csv), and the machine-readable quality snapshot is [data/phase6a_company_quality_summary.csv](../data/phase6a_company_quality_summary.csv).

| Measure | Actual Phase 6A starting point |
|---|---:|
| Unique companies / normalized domains | 537 / 537 |
| New unique companies in Phase 6A | 0 |
| Target / remaining | 5,000 / 4,463 |
| Discovery provenance rows | 540 |
| Website identities verified | 400 (saved Phase 4E/4F verification exports) |
| Known non-verified/ambiguous statuses among assessed additions | 60 |
| Legacy baseline companies with no status in saved Phase 4E/4F exports | 77 |
| Eligibility: ELIGIBLE / NEEDS_REVIEW / INELIGIBLE | 406 / 131 / 0 |
| GEO opportunity: HIGH / MEDIUM / LOW / UNKNOWN | 270 / 91 / 45 / 131* |
| Companies missing employee-size value | 68 |
| Companies pending human review | 537 |
| Companies with saved suitable email-type records (upper bound) | 151 |
| Companies with saved contact-form-type records (upper bound) | 64 |
| Contact discovery failure state | 7 |
| Saved website snapshots in error/block/robots-denied states | 33 |
| New-source permissions cleared for Phase 6A | 0 |

The 400 verified / 60 unresolved calculation combines Phase 4E (97 verified among IDs 78–198) and Phase 4F (303 verified among IDs 199–537). The original 77 are “not assessed in saved exports,” not unverified. `UNKNOWN` includes the 36 lowercase `unknown` GEO values normalized for reporting plus 95 uppercase `UNKNOWN` values. Eligibility and GEO figures are sourced directly from the Phase 4F DB and may not coincide with website-verified-only additions; consult the saved qualification export for per-company verification alignment. Contact counts are explicitly upper bounds by channel type, not a verified first-party/approved channel count; dashboard detail checks are stricter. Do not add email and form counts together because companies can have both.

The source composition remains the existing experiment: 477 YC-linked companies and 60 Greater Dalton Chamber-linked companies. Provenance rows include 30 original Consumer YC records and 447 in the generalized YC directory label, with 60 Chamber records. Source-linked companies may overlap; aggregate by company ID rather than summing row counts.

## Permission and yield result

The current [YC Terms of Use](https://www.ycombinator.com/legal) prohibit scraping/data-mining/similar extraction absent express authorization. Robots-allowed static routes observed in earlier phases do not establish the separate permission. Greater Dalton’s public directory and robots rules were examined in earlier phases, but no explicit automated reuse grant was found; the audit therefore marks permission unresolved. Full source findings and alternatives are in [phase6a_source_inventory.md](phase6a_source_inventory.md).

GLEIF is an important distinction: its official site states its data is CC0 and provides bulk LEI files, but its roughly 3.46 million legal-entity records do not provide the required domain, SMB, or GEO-relevance proof. That total is not a projected yield. It should only be considered for a separately approved identity-matching pilot, not direct ingestion. State filing registry queries that explicitly prohibit automation, public-company filings, and licensed aggregators likewise do not safely supply the target population.

Therefore the actual permission-cleared new-candidate yield is zero. Expected yield/runtime for 1,000/2,500/5,000 checkpoints is not estimable until a source with both suitable evidence fields and authorized access is approved. No staged checkpoint was fabricated. Existing Phase 4F discovery and website-processing measurements are historical Phase 4F values, not measured Phase 6A work.

## Processing and contact boundaries

No discovery/verify/qualify/contact command has performed network activity in Phase 6A. The added CLI is offline-only for `report` and `validate`; `discover`, `verify`, `qualify`, `discover-contacts` and `resume` fail closed until source permission is approved. This prevents the existing Phase 4F adapter from continuing to query YC or Chamber based only on historic robots observations. No website forms, emails, campaigns, research signups, approvals or campaign membership data were written.

If written permission or a suitable licensed/open dataset is obtained, the next implementation step should be an explicit source registry approval plus a dry-run candidate export, followed by deterministic domain/name/source-ID deduplication, an append-only import into the Phase 6A copy, 25-company verification/qualification batches, cached first-party contact discovery only for verified eligible companies, and a checkpoint export at 1,000/2,500/5,000. Each phase must preserve the original 537 records and separately label candidate, identity-verified, qualified, and contactable counts.

## Reproduction

Offline report and database integrity checks:

```sh
PYTHONPATH=src uv run python scripts/phase6a_scale_cohort.py report
PYTHONPATH=src uv run python scripts/phase6a_scale_cohort.py validate
```

The destination is an ignored, separate experimental file. Do not overwrite it. Future expansion commands currently fail closed by design:

```sh
PYTHONPATH=src uv run python scripts/phase6a_scale_cohort.py discover --dry-run
```

Dashboard (Phase 6A copy is the default; campaign store remains the Phase 5D sidecar):

```sh
PYTHONPATH=src uv run python scripts/phase5b_dashboard.py
```

Automated tests include Phase 6A source permission gating, offline report repeatability, copy/baseline integrity, dashboard read-only loading, and the existing campaign API behavior. Phase 6A did not send outreach or alter human approval states.
