# Phase 7 Performance Optimization and Bounded Benchmark

Date: 2026-10-16

## Outcome

The original serial candidate processor was stopped between completed chunks. Its checkpoints, evidence, run rows, and review decisions were preserved. Phase 7 then processed exactly 200 previously unprocessed candidates with a single locked runner and conservative four-company concurrency. The strict contactable count increased from 885 to 928 (+43); the benchmark did not approve contacts for outreach.

No full-pool crawl was resumed after the benchmark.

## Stopped-run reconciliation

At the stop point, the full pool had 57,664 unique canonical domains, 55,911 in the deterministic work order, 3,479 unique candidate records updated during the prior processing interval, 20,460 recorded HTTP attempts, and 885 strict contactable companies. The strict starting baseline after replaying existing saved Phase 6 evidence was 660, so the prior run had added 225 strict contactable companies. The original pre-replay CSV count was 315; the additional 345 were already-saved Phase 6 contact evidence, recovered offline rather than newly discovered.

The old run interval was 2026-10-15 22:44:12 UTC through 2026-10-16 03:55:20 UTC (about 5.19 hours). Its worker rows sum to 4,485 candidate attempts and 26,002.9 worker-seconds, while only 3,479 unique candidate records changed. Multiple resume processes overlapped before the run lock was added, producing duplicate work. The persisted request total is real, but the old per-window request/runtime attribution is contaminated by that overlap. The locked runner now rejects a second process for the same Phase 7 database.

Historical request totals: 14,745 successful, 5,723 failed, 1,795 retries. The prior saved failure analysis counted 278 DNS failures, 84 TLS failures, 184 HTTP 403 outcomes, 70 HTTP 404 outcomes, 263 robots denials, 29 timeouts, and 88 oversized responses (among other identity and transport outcomes). These are candidate/failure classifications, not a count of distinct HTTP response bodies.

The former implementation processed companies serially, with a one-second delay between requests; it fetched `robots.txt`, then an unused sitemap, then up to two first-party HTML pages. Each 25-candidate chunk created a collector/client, so connection pooling did not span the whole run. Identity decision and GEO qualification were not timed separately. The saved historical ledger did not record individual request latency, robots time, per-stage time, or effective concurrency. Those values cannot be reconstructed retrospectively and are not fabricated here.

Measured before-optimization throughput, using the unique updated-candidate count and outer run interval:

- 3,479 / 5.19 hours = about 671 candidates/hour.
- 225 net new strict contactable companies / 5.19 hours = about 43 contactable companies/hour.
- 20,460 / 3,479 = 5.88 HTTP attempts per unique candidate.
- 20,460 / 225 = 90.9 HTTP attempts per net new contactable company.
- 5,723 / 20,460 = 28.0% recorded failed attempts; 1,795 retries.

These historical rates include duplicate requests caused by overlapping runners and therefore are a conservative, imperfect baseline.

## Safe changes

- Kept a single OS-level run lock per Phase 7 database; it protects against duplicate runners but does not serialize requests inside one runner.
- Added a four-worker maximum for distinct candidate domains. Each company still fetches its own pages sequentially, honors the existing per-domain delay, robots rules, timeout, response cap, and retry budget. Phase 7 selection deduplicates canonical domains.
- Kept one shared `httpx.Client` and connection pool across each bounded 25-candidate work chunk; concurrent workers share it safely.
- Skipped the sitemap request for Phase 7 only. Sitemap content was not used by Phase 7 identity verification, GEO qualification, or contact extraction. Robots checks and the same homepage/contact-page evidence remain required. This avoided 188 actual sitemap requests in the benchmark; 12 candidates were stopped by robots outcomes before that point.
- Continued extracting contact evidence from already fetched pages; no additional contact-page request is made solely to parse an email/form found in that page.
- Added persisted request latency, stage timings, retry-delay, request-type, and peak-concurrency metrics to new `phase7_runs.metrics_json` rows. Older rows remain explicitly unmeasured.
- Added `--max-candidates` for reproducible bounded runs. Benchmark stop does not alter candidate selection or human-review state.

## 200-candidate benchmark

Benchmark interval: 2026-10-16 04:00:38–04:03:47 UTC. The CLI monotonic runtime was 192.38 seconds. All 200 candidates were previously unprocessed; candidate evidence and run metrics are saved in `data/phase7_candidates.db` (run IDs 119–126).

| Measure | Result |
|---|---:|
| Candidates processed | 200 |
| Strict new contactable companies | 43 |
| Identity verified | 96 (48.0%) |
| GEO eligible | 92 (46.0%) |
| Candidate status: ambiguous identity | 45 |
| DNS / TLS / HTTP 403 / HTTP 404 outcomes | 9 / 6 / 8 / 5 |
| Robots denied / timeout / other listed outcomes | 12 / 1 / 8 |
| Actual HTTP attempts | 622 |
| Successful / failed HTTP attempts | 505 / 117 (18.8% failed) |
| Retries | 34 |
| Sitemap requests avoided | 188 |
| Logical request latency, average / p95 | 572.72 ms / 1,852.78 ms |
| Observed peak request concurrency | 4 |
| Worker elapsed time / CLI wall runtime | 184.78 s / 192.38 s |
| Candidates/hour | about 3,743 |
| New strict contactable companies/hour | about 805 |
| Wall time per new strict contactable company | about 4.47 s |
| HTTP attempts / candidate; / new contactable company | 3.11; 14.47 |

`Logical request latency` measures one collector request operation (which may include a retry or redirects); HTTP attempts count actual hops. Failure outcomes above count affected candidate records, while the 117 is unsuccessful HTTP attempts. The rates are a small prioritized sample, not a guarantee for the remaining pool.

Measured worker-stage time in the benchmark (these are summed across concurrent workers and can exceed wall time): request time was 129.45 s for robots checks and 159.20 s for first-party pages; contact extraction took 8.89 s; identity decisions 0.056 s; GEO qualification 0.003 s; checkpoint persistence 1.78 s. Sitemap request time was zero. Rate-limit delays summed to 306.46 worker-seconds and retry backoff to 34.24 worker-seconds. Peak observed concurrency was four.

The unchanged first 500-record cohort was 41.4% identity-verified and 39.0% GEO-eligible; the new sample was 48.0% and 46.0%, respectively. These cohorts differ and the 200-candidate result does not establish a causal quality improvement. Verification rules and contactability thresholds were unchanged; all 43 contacts remain pending human review and `outreach_authorization=NOT_GRANTED`.

Of the 43 new companies, 28 had a suitable public business email and 17 had a confirmed form (two companies had both). The 200-candidate failure/outcome counts were 9 DNS, 6 TLS, 8 HTTP 403, 5 HTTP 404, 12 robots-denied, 1 timeout, 1 transient connection, 2 oversized response, 45 ambiguous identity, 2 mismatch/parked, 8 insufficient evidence, and 1 each invalid domain and invalid source URL, plus 3 redirect issues. Two request operations raised `ConnectTimeout`; these request-level errors are distinct from the one candidate classified as a timeout.

## Throughput interpretation and remaining estimate

The new sample processed about 5.6 times as many candidates/hour as the historical outer-interval rate. Strict contactability yield was 21.5% in this 200-candidate sample versus 6.47% net over the prior 3,479 updates. The sample is too small to use as a dependable pool-wide conversion rate.

There are now 928 strict contactable companies, leaving 4,072 to reach 5,000, with 52,232 candidates still unprocessed. If the 200-record sample repeated exactly, the arithmetic projection is roughly 18,940 candidates and 5.1 hours at observed benchmark throughput. If the earlier 6.47% net conversion persisted at the new throughput, the projection is about 16.8 hours—and the 52,232 remaining records would yield only about 3,379 more, short of target. These are scenario calculations, not commitments; continued batch metrics must recalibrate them.

## Resume command

The benchmark deliberately stopped after 200 records. To resume manually from the saved checkpoint with concurrency enabled, use:

```bash
PYTHONPATH=src uv run python scripts/phase7_contacts.py run \
  --batch-size 500 \
  --request-cap 28000 \
  --time-cap-seconds 30000 \
  --chunk-request-budget 500 \
  --concurrency 4
```

Caps are cumulative; this provides up to 6,918 additional HTTP attempts and 11,415 additional processing seconds from the benchmark checkpoint. The Phase 7 run lock prevents another process from using the same database simultaneously. No email or form submissions occur.
