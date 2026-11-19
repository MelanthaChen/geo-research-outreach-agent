# GEO Engine + Outreach performance baseline and Phase 2 changes

Date: 2026-11-19

## Scope and safety

Measured against the revisions verified live in Render on 2026-11-19: Outreach `8d901f5ce468086e3c3e616c87e30727a9359a61` and GEO Engine `1c303eb18ce9bd0df7885a44b50d8366c664c716`. Code changes are isolated in worktrees based on those exact revisions. Live Neon work was limited to read-only `EXPLAIN (FORMAT JSON)` and read-only Dashboard checks. Synthetic measurements used disposable SQLite databases only.

No invitation, consent, report, campaign manifest, personalization attempt, provider ledger, or delivery record was changed. No Exa/OpenAI call or email was made. The signed-in Dashboard showed the historical personalization authorization in `ATTENTION_REQUIRED`, with no worker currently running: 65/100 attempted, 60 PASS, 5 FAIL, 103 provider attempts, $0.77487 settled spend, and $0.08848 unresolved reserve. Its last runner heartbeat was 2026-11-13 00:01 UTC. This record and its unresolved reserve were left untouched.

## Phase 1: confirmed bottlenecks

### Outreach

- `service.list_candidates()` issued a per-candidate contacts query and invitation query, then additional per-candidate job and delivery lookups when an invitation existed. In the controlled 86,000-candidate baseline, a page request used **209 SQL statements**.
- `service.generate()` repeated contact, suppression, and duplicate-invitation lookups per selected candidate. A 150-candidate review package used **1,363 SQL statements** and executed inside one synchronous serialized transaction.
- The deployed UI requests 50 rows at a time, but uses offset paging. The application code did not select server-side cursors; evidence/personalization details are loaded separately on the detail view.

### Live Neon and Render observations

- Neon read-only `EXPLAIN (FORMAT JSON)` of the deployed default first-page ordering showed `Parallel Seq Scan → Sort (name, id) → Gather Merge → Limit`. The database lacked an index that matched this ordering. The Neon UI reported 59 ms for the explain request, while the plan itself reports execution and planning time as unavailable; **59 ms is not query execution latency**.
- `EXPLAIN ANALYZE` was not run against the 86k-row live table to avoid executing a production scan. No live P95 or transaction-time histogram is available in the application logs; Uvicorn access lines contain method, path, and status but no duration.
- Render exposes 512 MB memory and 0.15 CPU limits for both services. CPU and memory time-series are marked as paid-plan metrics and could not be inspected. Render warns that the free instances can spin down and delay the first request by 50 seconds or more. This is a separate cold-start risk from database access.
- The current code executes the GEO Engine `outreach-advance` pipeline synchronously through audit, retrieval, experiment execution, and result export. Its durable records endpoint previously loaded every `research_result_export` payload before filtering to one profile. Provider evidence and model result access also performed global record-kind scans.

### Reproducible local synthetic benchmark

The benchmark script `scripts/perf_data_access.py` creates a new local SQLite database, seeds 86,000 synthetic candidates and 150 eligible contacts, records SQL statement counts, prepares a review-only invitation package, and makes zero provider/email calls. It refuses to overwrite an existing path. Run it once against the deployed baseline worktree and once against the Phase 2 worktree:

```bash
python3 scripts/perf_data_access.py --source-root /tmp/geo-outreach-perf-baseline-8d --db /tmp/geo-outreach-perf-baseline-8d.db --candidates 86000 --eligible 150 --samples 30
python3 scripts/perf_data_access.py --source-root /tmp/geo-outreach-perf-v771ddf --db /tmp/geo-outreach-perf-phase2-8d.db --candidates 86000 --eligible 150 --samples 30
```

The baseline worktree is created from the exact live Outreach SHA above; the optimized worktree contains the Phase 2 changes. Each run uses its own fresh local database. Measurements on this host:

| Synthetic workload | Deployed baseline | Phase 2 | Change |
|---|---:|---:|---:|
| Candidate list, 50 rows, 30 samples, SQL statements/request | 209 | 6 | 97.1% fewer |
| Candidate list median | 138.37 ms | 55.96 ms | 59.6% lower |
| Candidate list P95 (nearest-rank) | 158.93 ms | 63.66 ms | 59.9% lower |
| Prepare 150 invitations, SQL statements | 1,363 | 17 | 98.8% fewer |
| Prepare 150 invitations, elapsed time | 523.96 ms | 311.40 ms | 40.6% lower |
| Review package size | 121,772 bytes | 121,980 bytes | 208 bytes larger |

Both generated packages remained `PENDING_REVIEW`. These are local SQLite timings, not Neon/Render production latency. Host load and filesystem cache can change elapsed times; SQL-count results are deterministic for this fixture.

The Engine script `backend/scripts/perf_durable_store_lookup.py` seeds 100,000 synthetic durable result records in local SQLite and compares a full record-kind scan with an indexed profile/version lookup (30 samples). It reported 100,000 versus 1 materialized row; median 122.15 ms versus 0.17 ms, and P95 124.71 ms versus 0.30 ms. The run made zero provider calls and did not use live data.

## Phase 2: implemented data-access changes

### Outreach

- Candidate list rows omit the large `personalization` JSON field; the two fixed test fixtures load it only when needed for the explicit fixture-readiness check.
- Contacts and invitations are loaded for a page in bounded queries; research and active-report delivery status are loaded in batched lookups instead of per row.
- Generation preloads candidate contacts, suppressions, existing candidate invitations, and normalized existing recipients in 5,000-ID batches. Invitation rows are inserted with one executemany operation.
- Candidate UI paging now sends a stable keyset cursor over `(name, id)`. Page-number/offset remains supported for older API clients. Malformed cursors are rejected; filters reset the cursor stack.
- Migration v12 adds `ix_candidates_name_id` (migration 11 on the deployed base already belongs to the historical-screening campaign). PostgreSQL creates it with `CREATE INDEX CONCURRENTLY`, guarded by a dedicated session advisory lock; SQLite creates the equivalent regular index. This preserves ongoing writes during the PostgreSQL index build.

### GEO Engine

- Durable result reads are scoped by indexed `kind`, `profile_id`, and `profile_version` instead of loading every research result into Python.
- Idempotent durable record writes use the primary-key lookup. Retrieval checkpoint recovery fetches only the exact job-derived record-ID prefix.
- Provider budget and raw-response reads are scoped to the exact job’s deterministic record-ID prefix, preserving all ledger entries and response archives while avoiding unrelated job payloads.
- No Engine schema change is required; migration 0029 already indexes `kind` and `profile_id`, and `id` is the primary key.

## Regression results

- Outreach focused platform, data-access, and report-version tests on the latest base: **126 passed**.
- Outreach browser JavaScript syntax and `git diff --check`: passed.
- GEO Engine durable job/budget, integration auth, and research-result provenance tests: **40 passed**.
- GEO Engine integration pipeline, exact-job allowlist, and GPT compatibility tests: **17 passed**.
- Tests use synthetic SQLite or mocks. No live worker, provider, invitation send, report delivery, or campaign was invoked.

## Database migration and rollback

Outreach migration v12 is additive and reversible. On PostgreSQL, the app builds the ordering index concurrently after the transactional migrations and records version 12 only after the index is valid. SQLite test/dev databases use `CREATE INDEX IF NOT EXISTS`.

Rollback order: deploy the previous Outreach code; then, if removing the index is required, run `DROP INDEX CONCURRENTLY IF EXISTS outreach_v2.ix_candidates_name_id` and delete only schema migration row 12 from `outreach_v2.schema_migrations`. No candidate or workflow rows are updated by this migration. Engine has no schema migration in Phase 2.

## Phase 2 live migration verification — 2026-11-19

After recovery commit `1bac9a1b7c60f24872fbdc78dfabbe6f7b8708d5` became live, a read-only Neon catalog query returned one row:

| version | migration | schema | index | valid | ready | definition |
|---:|---|---|---|---|---|---|
| 12 | `candidate-name-id-pagination-index` | `outreach_v2` | `ix_candidates_name_id` | true | true | `CREATE INDEX ix_candidates_name_id ON outreach_v2.candidates USING btree (name, id)` |

This verifies both the migration ledger and physical index state. The recovery code checks the index catalog instead of trusting only the migration row, and creates the index without schema-qualifying the index identifier in PostgreSQL's `CREATE INDEX CONCURRENTLY` grammar.

## Phase 3 initial implementation — durable invitation generation

The authenticated bulk Generate API now performs recipient-preview hash validation, stores an immutable candidate/recipient/template/questionnaire snapshot, and returns a durable batch ID. A dedicated manually dispatched GitHub Actions workflow calls a token-protected invitation-generation endpoint; each invocation claims at most four chunks and commits at most 25 invitations per chunk. It has no recurring schedule, so deployment alone cannot invoke it. Both chunk size and chunks per tick have hard upper bounds. The new worker is fail-closed behind `OUTREACH_INVITATION_GENERATION_ENABLED`, which defaults to off; the live environment was not changed during this work. Each chunk rechecks current candidate fingerprint, contact authorization, suppression, and duplicate invitation/recipient state before writing. Rendering or database failure rolls back the chunk; the immutable selection remains `RETRY_PENDING` and resumable, with recovery attempts capped separately from successful chunk count. The final ZIP is assembled after all invitation transactions complete, outside the database transaction, and human package review remains required. Generation never sends email. The new worker cannot ingest Forms, claim research jobs, call providers, or deliver reports.

Schema migration v13 adds `invitation_generation_batches` and `invitation_generation_items`. Existing invitations, candidates, consent, and delivery records are not changed by the migration. Rollback is expand/contract: deploy the prior app only after queued generation batches are drained; retain the additive tables until any queued data is intentionally resolved. Do not drop the new tables as part of application rollback.

### Reproducible local synthetic measurements

`scripts/perf_data_access.py` now has `--async-chunks`. Runs used isolated SQLite databases, 86,000 synthetic candidates, zero providers, zero emails, and a process-style database dispose/reopen between every worker chunk. On this host:

| Selected eligible recipients | Synchronous request time | Durable enqueue response | Worker chunks | Worker total | Final package |
|---:|---:|---:|---:|---:|---:|
| 150 | 338.58 ms | 33.52 ms | 6 × 25 over 2 ticks | 282.62 ms | 122,041 bytes |
| 1,000 | 2,387.98 ms | 212.01 ms | 40 × 25 over 10 ticks | 2,735.11 ms | 340,728 bytes |

The 150-recipient async path completed in 316.14 ms end to end; the 1,000 path completed in 2,947.12 ms. The API response portion is 90.1% and 91.1% lower, respectively. The 1,000-row total is 23.4% slower in this local synthetic run because of chunk transaction and revalidation overhead, even though the request no longer waits for it; this is a throughput tradeoff to monitor. Local list timings remained 6 SQL statements/request, with median/P95 56.08/61.89 ms at the 150 run and 60.98/64.91 ms at the 1,000 run. Package states remained `PENDING_REVIEW`; persisted invitation counts were exactly 150 and 1,000. The benchmark records worker chunk/tick timings and persisted status and refuses to overwrite an existing database path. A separate regression test disposes and reopens the database between committed chunks to verify restart recovery.

These are synthetic SQLite measurements, not Neon/Render production latency. The prior same-host Phase 2 reference for 150 rows was 311.40 ms and 17 SQL statements; async end-to-end was comparable while moving most work off the request. For 1,000 rows the new path uses 40 small durable transactions rather than one long synchronous generation transaction.

### Correctness gate and remaining limits

- `tests/rebuild` excluding pre-existing `test_personalization_quality.py`: 188 passed. The excluded file fails at collection on the exact live base `1bac9a1b7c60f24872fbdc78dfabbe6f7b8708d5` because it imports missing `outreach_v2.service.readiness`; this change does not touch that API.
- Focused `test_platform.py` plus `test_performance_data_access.py`: 123 passed after adding chunking, idempotency, restart, stale-snapshot, and bounded failure-recovery coverage.
- JavaScript syntax check and `git diff --check`: passed.
- No live candidate count, personalization, contact, invitation, provider spend, consent, report, or delivery data was changed during verification.

Production request P95, `EXPLAIN (ANALYZE, BUFFERS)` on the full candidate table, connection-pool utilization, transaction duration, and Render CPU/memory time series remain unmeasured. Do not interpret the synthetic numbers as production SLO evidence. GEO Engine Phase 4 work—stage-level measurement, bounded page acquisition, and research checkpoint/restart profiling—remains separate and is not included in this Outreach change.
