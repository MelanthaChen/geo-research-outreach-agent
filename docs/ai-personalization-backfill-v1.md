# AI personalization backfill v1

The normal Outreach worker now has a semantic personalization path. It is deliberately **disabled by default**. When disabled, the worker records `DISABLED`, performs no website fetches, makes no model calls, and does not change candidate or invitation records. The deterministic analyzer is not used as a silent fallback.

## Execution path

1. The personalization-only command selects only IDs in an explicit authorized cohort, then applies the verified, eligible, unsuppressed, non-fixture, one-first-party-email, and no-invitation checks. The paid path fails closed when the cohort is empty or exceeds five IDs. Existing database leases, `FOR UPDATE SKIP LOCKED`, domain cooldowns, and retry windows remain in force. The recurring full worker keeps paid personalization disabled unless its separate scheduled flag is explicitly enabled, and it still requires the same exact cohort.
2. Website fetch and visible-content extraction run before a model call. The evidence selector deduplicates excerpts, retains their exact URL/source provenance, filters event/partner/customer material, and rejects code-contaminated or insufficient evidence.
3. The OpenAI adapter is restricted to `gpt-5.4-mini-2026-03-17`, counts the complete prompt/schema/evidence with the pinned tokenizer, prices the worst-case request against the pinned published price schedule, uses one HTTPS request with retries/tools/fallbacks disabled and a bounded 20-second response timeout, and verifies response schema and usage. No environment-selected endpoint or model is accepted.
4. The application composes recipient fields from cited source-derived facts. Model prose remains in the version audit; uncertain or unsupported text is not promoted into recipient copy. `template.validate()` and both quality decisions run before a candidate becomes READY.
5. A durable run row, one-per-evidence-fingerprint attempt row, and immutable personalization version rows retain usage, costs, request ID, raw provider response, output, quality results, and legacy snapshots. Completed responses can be replayed locally without another model request. Ambiguous provider failures retain the worst-case cost reservation and block automatic retry for that evidence fingerprint.
6. Invitation generation reruns the quality checks and requires `personalization_analysis_version=semantic-openai-v1`, a positive version number, and quality PASS. Historical READY-only records cannot bypass the gate. The invitation/report delivery workflows are otherwise unchanged; invitation sending remains operator initiated and the exact-DOCX report approval gate remains mandatory.

## Runtime settings

| Setting | Safe default | Purpose |
| --- | --- | --- |
| `OUTREACH_AI_PERSONALIZATION_ENABLED` | `false` | Master switch. Must be explicitly enabled for an approved pilot. |
| `OUTREACH_AI_PERSONALIZATION_MODEL` | `gpt-5.4-mini-2026-03-17` | Exact allowlisted model; any other value fails closed. |
| `OUTREACH_AI_PERSONALIZATION_MAX_CANDIDATES_PER_RUN` | `5` | Sequential cap and hard maximum five for this pilot. |
| `OUTREACH_AI_PERSONALIZATION_RUN_BUDGET_USD` | `0` | Positive budget is required before processing. |
| `OUTREACH_AI_PERSONALIZATION_CUMULATIVE_BUDGET_USD` | `0` | Positive lifetime ceiling is required before processing. |
| `OUTREACH_AI_PERSONALIZATION_MAX_COST_PER_CANDIDATE_USD` | `0` | Must be positive and at most $0.05; adapter's current worst-case preflight is below $0.05. |
| `OUTREACH_AI_PERSONALIZATION_REPROCESS_ENABLED` | `false` | Enables explicit reprocessing of selected uninvited legacy READY records only. |
| `OUTREACH_AI_PERSONALIZATION_REPROCESS_CANDIDATE_IDS` | empty | Optional comma-separated candidate IDs; selection is bounded by the run cap and still passes all current eligibility checks. |
| `OUTREACH_AI_PERSONALIZATION_COHORT_CANDIDATE_IDS` | empty | Exact pilot allowlist. Paid execution fails closed without it, never fills unused slots from the database, and cannot exceed five IDs. |
| `OUTREACH_AI_PERSONALIZATION_SCHEDULED_ENABLED` | `false` | Keeps paid personalization out of the recurring full worker; use the personalization-only command for an approved bounded pilot. |
| `OUTREACH_AI_PERSONALIZATION_OPERATION_ID` | `initial` | Idempotency key for one evidence fingerprint. Repeated ticks replay/skip this operation. A deliberate retry after an uncertain/failed request requires selected IDs, the reprocess switch, a new unique operation ID, and enough new budget; it never silently retries. |

`OPENAI_API_KEY` must already be securely configured in the runtime. It is never stored in the database or included in run diagnostics. The existing `tiktoken` dependency is required; missing tokenizer or uncertain preflight blocks execution before the provider request.

At the adapter's 15,000 input / 6,000 output token ceilings, the estimated maximum is $0.03825 per request under its pinned pricing assumptions. Five requests therefore reserve at most $0.19125 under these limits, within the separately authorized $0.25 pilot cap. These are planning ceilings, not a forecast of actual usage. No spend is enabled by the checked-in settings: all budget defaults are zero, the master switch is off, the cohort is empty, and scheduled execution is off.

## Schema and release

Migration 6 is additive: three nullable/defaulted fields on `candidates` identify the current semantic analysis version/fingerprint, plus `personalization_runs`, `personalization_attempts`, and `personalization_versions`. It does not update candidate rows, delete/rename tables, or touch invitations, consent, research jobs, reports, deliveries, source records, or the saved Lagoon preview. A read-only inspection of the live Neon `schema_migrations` table on 2026-10-29 confirmed versions 1–6, including `audited-semantic-personalization-ledger`; the live migration head is 6.

Rollback means disabling the feature switch and reverting the application release. Keep additive migration tables/columns and their audit rows; do not drop them as part of rollback. No paid pilot should start until an owner authorizes its candidate count and dollar budget, approves the runtime settings, and confirms the key is available to that runtime.

## Single-release checklist (no deployment performed)

1. Before a separately authorized release, confirm the current staging SHA and review the already-applied Neon migration 6; take a new database backup only if the release plan requires one.
2. Deploy only the consolidated application SHA. Keep `OUTREACH_AI_PERSONALIZATION_ENABLED=false`, both budgets at zero, the exact cohort empty, `OUTREACH_AI_PERSONALIZATION_REPROCESS_ENABLED=false`, and scheduled execution off until the exact five-company pilot is approved.
3. Verify `/health`, administrator login, the existing Lagoon preview and package, candidate/dashboard queries, and migration 6 on staging. Confirm candidate, invitation, submission, research, report, and delivery counts did not change during deployment verification.
4. For the five-company pilot, select exactly five uninvited eligible IDs using read-only Neon access. The maximum request estimate is $0.03825 each, $0.19125 across five maximum-size requests, with a hard cumulative ceiling of $0.25 and a $0.05 per-candidate cap. Require approval of the exact cohort and keep the exact cohort configured for every run; an empty or changed cohort fails closed.
5. After the approved pilot, inspect actual cost, outcomes, evidence, rejected/uncertain claims, complete invitation previews, and package integrity. Keep production invitation creation and external sending blocked until separately authorized. The existing report DOCX hash approval gate remains independent and unchanged.
