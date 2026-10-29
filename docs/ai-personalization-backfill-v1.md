# AI personalization backfill v1

The normal Outreach worker now has a semantic personalization path. It is deliberately **disabled by default**. When disabled, the worker records `DISABLED`, performs no website fetches, makes no model calls, and does not change candidate or invitation records. The deterministic analyzer is not used as a silent fallback.

## Execution path

1. The worker selects only verified, eligible, unsuppressed, non-fixture companies with exactly one qualifying first-party email and no invitation. Existing database leases, `FOR UPDATE SKIP LOCKED`, domain cooldowns, and retry windows remain in force.
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
| `OUTREACH_AI_PERSONALIZATION_MAX_CANDIDATES_PER_RUN` | `5` | Sequential cap, hard maximum 10; actual work also stops at the existing 25-second worker request budget. |
| `OUTREACH_AI_PERSONALIZATION_RUN_BUDGET_USD` | `0` | Positive budget is required before processing. |
| `OUTREACH_AI_PERSONALIZATION_CUMULATIVE_BUDGET_USD` | `0` | Positive lifetime ceiling is required before processing. |
| `OUTREACH_AI_PERSONALIZATION_MAX_COST_PER_CANDIDATE_USD` | `0` | Must be positive and at most $0.05; adapter's current worst-case preflight is below $0.05. |
| `OUTREACH_AI_PERSONALIZATION_REPROCESS_ENABLED` | `false` | Enables explicit reprocessing of selected uninvited legacy READY records only. |
| `OUTREACH_AI_PERSONALIZATION_REPROCESS_CANDIDATE_IDS` | empty | Optional comma-separated candidate IDs; selection is bounded by the run cap and still passes all current eligibility checks. |
| `OUTREACH_AI_PERSONALIZATION_OPERATION_ID` | `initial` | Idempotency key for one evidence fingerprint. Repeated ticks replay/skip this operation. A deliberate retry after an uncertain/failed request requires selected IDs, the reprocess switch, a new unique operation ID, and enough new budget; it never silently retries. |

`OPENAI_API_KEY` must already be securely configured in the runtime. It is never stored in the database or included in run diagnostics. The existing `tiktoken` dependency is required; missing tokenizer or uncertain preflight blocks execution before the provider request.

At the adapter's 15,000 input / 6,000 output token ceilings, the estimated maximum is $0.03825 per request under its pinned pricing assumptions. Thus a 5-company run needs at least $0.19125 of run budget and a 10-company run at least $0.3825, while the cumulative budget remains an independent cap. These are planning ceilings, not a forecast of actual usage. No spend is authorized by the checked-in settings: all budget defaults are zero and the master switch is off.

## Schema and release

Migration 6 is additive: three nullable/defaulted fields on `candidates` identify the current semantic analysis version/fingerprint, plus `personalization_runs`, `personalization_attempts`, and `personalization_versions`. It does not update candidate rows, delete/rename tables, or touch invitations, consent, research jobs, reports, deliveries, source records, or the saved Lagoon preview. Existing migrations 4 and 5 from the local governance/quality milestone remain in the chain; no migration has been applied to Neon.

Rollback means disabling the feature switch and reverting the application release. Keep additive migration tables/columns and their audit rows; do not drop them as part of rollback. No paid pilot should start until an owner authorizes its candidate count and dollar budget, approves the runtime settings, and confirms the key is available to that runtime.

## Single-release checklist (no deployment performed)

1. Before a separately authorized release, confirm the current staging SHA and take the requested Neon backup; verify the schema migration head is v5 and review the isolated v1–v6 upgrade results.
2. Deploy only the consolidated application SHA. Keep `OUTREACH_AI_PERSONALIZATION_ENABLED=false`, both budgets at zero, and `OUTREACH_AI_PERSONALIZATION_REPROCESS_ENABLED=false`; there is no automatic fallback to deterministic generation.
3. Verify `/health`, administrator login, the existing Lagoon preview and package, candidate/dashboard queries, worker invocation, and migration 6 on staging. Confirm candidate, invitation, submission, research, report, and delivery counts did not change during deployment verification.
4. For a later pilot, first select and review 5–10 uninvited eligible candidate IDs using read-only Neon access. Propose a USD $0.50 total ceiling for ten requests ($0.03825 worst-case per request under this adapter's caps), with a $0.05 per-candidate hard cap. The owner must explicitly approve that total and the selected IDs before the feature switch, positive budgets, or historical reprocessing are enabled.
5. After the approved pilot, inspect actual cost, outcomes, evidence, rejected/uncertain claims, complete invitation previews, and package integrity. Keep production invitation creation and external sending blocked until separately authorized. The existing report DOCX hash approval gate remains independent and unchanged.
