# Phase 4G: Human Review Prioritization & Scale Readiness

## Outcome

Phase 4G generated a deterministic, offline review queue for the existing 537-company Phase 4F experiment. It did not expand discovery, crawl websites, enrich contacts, send outreach, or change qualification rules. The Phase 4F SQLite database was opened read-only; the frozen 77-company database and Phase 4D source copy remain unchanged.

The master export contains one row per stable company ID. It orders records by verified website identity, existing eligibility, GEO opportunity, existing research-priority tier, evidence confidence, then company ID. It reuses the stored research-priority values and does not use contact availability as a sort/scoring input. Independent columns separate website identity, eligibility, GEO, contact-channel, company review, outreach readiness, and outreach approval.

## Review workload observed in the 537-company cohort

| Review category | Current queue | Interpretation |
| --- | ---: | --- |
| Company decision | 537 | Every company remains `PENDING`; no stored human decision was rewritten. |
| Website identity | 137 | 60 unresolved identities among the 460 Phase 4E/4F additions, plus 77 original baseline companies not rechecked in those phases. |
| Eligibility | 131 | Existing machine eligibility is `NEEDS_REVIEW`. |
| GEO opportunity | 131 | Existing machine GEO opportunity is `UNKNOWN`. |
| Contact-channel review | 8 companies | Suitable channel evidence is pending and company identity/eligibility gates are satisfied. |
| Pending suitable channel records | 50 | These are contact records across 33 companies; 25 companies are waiting on identity review, and 8 are currently actionable for channel review. |

Of the 460 post-baseline additions, 400 have a saved `VERIFIED` website identity and 60 have unresolved known identity outcomes. The remaining 77 original baseline companies are marked `BASELINE_NOT_RECHECKED`, not misrepresented as ambiguous or newly verified. The unresolved-identity view contains 60 known cases; a separate 77-row view identifies legacy websites not rechecked. The high-priority company-review view contains 235 records that are identity-verified, machine-eligible, `HIGH` research priority, and still pending human company review. The missing-qualification-evidence view contains 228 rows; it also calls out employee-size gaps, even though size is not an eligibility or priority threshold.

There are **zero outreach objects** in the experimental database. Every queue row has `outreach_approval_status=NOT_GRANTED`; current outreach readiness is blocked by pending company review or earlier evidence gates. Machine `VERIFIED`, `ELIGIBLE`, and GEO scores are never labeled human-approved. The existing reviewer notes, reviewer identity, and review status are copied through unchanged.

## Source diversity and sampling bias

| Source membership | Unique companies | Share | HIGH priority | HIGH within source |
| --- | ---: | ---: | ---: | ---: |
| YC public directory | 477 | 88.8% | 246 | 51.6% |
| Greater Dalton Chamber | 60 | 11.2% | 24 | 40.0% |
| Both sources | 0 | 0.0% | — | — |

These are unique company memberships, not raw provenance-row counts. Company counts sum to 537 and there is no cross-source overlap in this cohort.

Industry labels are uneven: the existing normalized `Other` bucket contains 330 companies (61.5%), 68 (12.7%) are `Unknown`, then Healthcare has 36 (6.7%), SaaS / Software 32 (6.0%), and Education 26 (4.8%). Geography is similarly concentrated: 291 companies (54.2%) list San Francisco, 59 (11.0%) New York City, and 46 (8.6%) Dalton, Georgia. Company-size evidence is present for 451 (84.0%) and missing for 86 (16.0%). The Chamber has no employee-count evidence for any of its 60 companies; YC has a directory-reported count for 451 of 477.

This is a source-selected research cohort, not a representative sample of SMBs. Its high YC share, YC category-route selection, San Francisco concentration, local Chamber footprint, normalized `Other` industry bucket, and source-specific size metadata create clear selection and measurement biases. Phase 4G makes no population-level claim and does not infer company size from age, appearance, industry, or source membership.

Current stored qualification distributions are 406 `ELIGIBLE` and 131 `NEEDS_REVIEW`; GEO opportunity is 270 `HIGH`, 91 `MEDIUM`, 45 `LOW`, and 131 `UNKNOWN`. Research priority has the corresponding stored distribution: 270 `HIGH`, 91 `MEDIUM`, 45 `LOW`, and 131 `NEEDS_REVIEW`. No thresholds or decisions were recalculated for this review export.

## Runtime instrumentation and scale projection

The website collector now records, for future bounded runs: outbound request attempts (including each followed redirect), 2xx/3xx responses, failed responses or request errors, retry attempts, cache hits, per-company elapsed seconds, and total batch elapsed seconds. Cached companies add a cache hit and zero website requests. Phase 4F aggregation now preserves the numeric counters and per-company timings instead of discarding or treating the timing map as a count. These counters have deterministic mock-transport tests.

No live crawl was run to backfill historical request metrics. The completed Phase 4F first pass processed 339 additions in about 1,367 seconds (roughly 4.03 seconds per company). Saved page/robots/sitemap evidence supports a **minimum inferred** 1,265 request calls (3.73 per company); actual attempts may be higher because retries and redirects were not recorded at the time. The repeat processing pass took 0.64 seconds, reported 339 cache hits, and made zero new fetches; that runtime includes qualification/export work and is not a pure website-request benchmark.

| Target companies | Website processing runtime estimate | Minimum website-request estimate | Identity review tasks | Eligibility tasks | GEO tasks | Company decisions |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000 | 67.21 min | 3,732 | 130 | 233 | 233 | 1,000 |
| 2,500 | 168.04 min | 9,329 | 326 | 582 | 582 | 2,500 |
| 5,000 | 336.07 min | 18,658 | 652 | 1,163 | 1,163 | 5,000 |

Runtime and minimum request counts are linear extrapolations from one Phase 4F first pass; they exclude discovery and contact work and are not an SLA. Review task rates use the 460 Phase 4E/4F additions (IDs 78–537), excluding the 77 legacy baseline records that were not identity-rechecked: 60/460 identity cases and 107/460 each for eligibility and GEO review. All additions remain pending company decision. Contact-channel review is **not extrapolated** because contact discovery intentionally sampled only ten companies. Human review time in minutes/hours is not estimated because no review-duration measurements exist. Task counts overlap and must not be summed as unique companies or labor hours.

The scale table also includes the historical 339-company cached replay and current full-cohort review workload as a separate observation. Estimates should be recalibrated after a representative, instrumented batch at the next scale; no 5,000-company run is implied by these calculations.

## Exports and reproduction

The main spreadsheet-compatible queue is `data/phase4g_review_queue.csv`. Companion views are:

- `data/phase4g_high_priority_company_review.csv` — verified, eligible, HIGH-priority companies awaiting company review.
- `data/phase4g_ambiguous_identity_review.csv` — 60 known unresolved identity cases (including ambiguous, insufficient, failed, blocked, robots-denied, third-party, and mismatch outcomes; these statuses remain distinct in the master queue).
- `data/phase4g_legacy_identity_unassessed.csv` — 77 original baseline companies not rechecked by Phase 4E/4F.
- `data/phase4g_missing_qualification_evidence.csv` — missing qualification and descriptive evidence, including size.
- `data/phase4g_contact_channel_review.csv` — verified and eligible companies with a pending suitable channel-review task.
- `data/phase4g_source_diversity.csv` — source, industry, exact stored geography, size-evidence coverage, and priority breakdowns.
- `data/phase4g_scale_projection.csv` — measured inputs, extrapolations, denominators, and limitations.

Regenerate all outputs without network access or database writes:

```sh
PYTHONPATH=src uv run python scripts/phase4g_review_readiness.py
```

The full test suite passes (**126 tests**). `git diff --check` is clean. Repeated export generation is byte-stable; the review queue has 537 unique company IDs and all outreach approvals remain `NOT_GRANTED`. The frozen database SHA-256 remains `b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6`; the Phase 4D database SHA-256 remains `e41871d286b8af764b0ccd7bb390c5c693aa6fb5d13f4431bcaf4c3d193995ad`. Phase 4F's original 198 company rows, 201 provenance records, and 351 website snapshots compare equal to Phase 4D.
