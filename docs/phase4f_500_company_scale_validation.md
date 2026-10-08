# Phase 4F: Scale Unified Discovery and Qualification Toward 500 Companies

## Outcome

The isolated experiment now contains **537 unique companies**: the preserved Phase 4D/4E cohort of 198 plus **339 new records**. The target was crossed at a category boundary, not forced. New additions are 336 YC directory companies and 3 Greater Dalton Chamber companies. The original 77-company database was not used as a write target.

The existing workflow verified **303/339 new website identities** (89.4%) and passed only those IDs to the existing qualification rules. Of the 339 additions, 277 are `ELIGIBLE`, 62 remain `NEEDS_REVIEW`, and none are `INELIGIBLE`. GEO opportunity is `HIGH` for 185, `MEDIUM` for 63, `LOW` for 29 and `UNKNOWN` for 62. All 339 new records remain `PENDING` for human review. Eligibility, GEO opportunity, research priority, website identity, and outreach readiness remain separate exported fields.

## Discovery sources and controlled expansion

### YC official public directory

The only YC source used was its official static industry directory; no OSS dataset or third-party mirror was used. The live [YC `robots.txt`](https://www.ycombinator.com/robots.txt) disallows query-driven `/companies?...` routes but allows static pages. The workflow read nine static routes in a deterministic order and stopped after `marketing` crossed the target:

| Static category | New unique companies |
| --- | ---: |
| B2B | 22 |
| SaaS | 35 |
| Fintech | 40 |
| Healthcare | 40 |
| Education | 50 |
| E-commerce | 41 |
| Developer tools | 37 |
| Artificial intelligence | 25 |
| Marketing | 46 |
| **Total** | **336** |

Each category was capped at 50 accepted detail records. Profile URLs already present in the experimental database or already seen in the current sweep were skipped before requesting a detail page. The adapter retains each YC company ID, profile URL, and category route as provenance. Ingestion reported zero duplicate-domain candidates among the 336 accepted rows; the final company table has no duplicate normalized domains. Because raw category-link skip counts were not instrumented in this completed run, the exact rate of overlapping source links is unknown and is not presented as zero.

The prior 4D cohort included 141 YC-linked companies. Phase 4F raised that to 477 YC-linked companies across the full experiment. This proves 336 additional accessible records from the nine routes, not the remaining source-wide capacity. The public [Consumer directory](https://www.ycombinator.com/companies/industry/consumer) visibly indicates dynamic “load more” behavior; no disallowed query route or browser automation was used to fetch hidden pages. Further category expansion should remain bounded and avoid repeating profile URLs.

### Greater Dalton Chamber

The existing Greater Dalton Chamber GrowthZone source was also checked, with no assumptions based on its total membership. The public [directory landing page](https://business.daltonchamber.org/list/) exposes alphabetical routes and category links; the live [Chamber `robots.txt`](https://business.daltonchamber.org/robots.txt) allows the sampled `/list/Search/` pages. Three relevant category routes were sampled: Technology/Telecommunications/Electronics, Health Care/Wellness/Human Services, and Advertising/Marketing/Media/Creative Services. They contained respectively 0, 2, and 1 website-bearing cards accepted by the adapter. Those three records were net new; no pagination links were found in these sampled category pages. The Technology sample exposed no website-bearing cards. Category routes use a newer GrowthZone card structure than the original adapter, so the existing adapter was extended to recognize that markup and read only cards with an explicit “Visit Website” link. It did not open member detail, email, or contact endpoints.

Three Chamber companies were added, bringing Chamber-linked domains from 57 to 60. The Chamber's source-wide capacity and pagination beyond these routes remain unmeasured; no total-member estimate is inferred. Public listing visibility and robots permission are access observations, not a legal opinion or data-reuse license. No automated-reuse grant was established in the earlier Phase 4D audit.

### Duplicate and provenance accounting

The expansion added 339 company rows and 339 new provenance rows to the original 198 companies / 201 provenance rows, for **537 companies and 540 provenance rows** total. The three duplicate-domain groups that already existed as multiple provenance records remain intact; there are **zero duplicate company-domain rows**. Re-importing the three sampled Chamber category routes created zero new companies and zero new provenance rows (three candidates were recognized as duplicates with existing provenance).

## Website processing and resume behavior

All 339 additions were processed sequentially in batches of at most 25 with the existing crawler/cache. Effective settings: 15-second timeout, one retry after an initial transient failure, one-second request delay, 2 MB response limit, at most two pages per domain, robots checks, and no anti-bot bypass. Each company snapshot is committed before the next company is processed. Unexpected per-company exceptions are converted to a `FETCH_FAILED` snapshot so the rest of the batch continues. The retry/cache behavior is unchanged from the existing crawler; discovery category checkpoints commit independently.

| Website identity status | YC (336) | Chamber (3) | Total |
| --- | ---: | ---: | ---: |
| `VERIFIED` | 303 | 0 | 303 |
| `AMBIGUOUS_IDENTITY` | 17 | 2 | 19 |
| `ACCESS_BLOCKED` | 4 | 0 | 4 |
| `FETCH_FAILED` | 6 | 1 | 7 |
| `ROBOTS_DENIED` | 1 | 0 | 1 |
| `INSUFFICIENT_EVIDENCE` | 4 | 0 | 4 |
| `REJECTED_MISMATCH` | 1 | 0 | 1 |
| `THIRD_PARTY_URL` | 0 | 0 | 0 |

The existing identity checker requires saved company/domain/title/content alignment; HTTP success alone is insufficient. The one mismatch had explicit unrelated/repurposed evidence. Blocked, robots-denied, and failed sites were not treated as identity mismatches. 300 of the new records had at least 500 visible text characters, a content indicator distinct from identity verification.

Initial full website processing covered 336 domains in 14 batches (325 fetched, 4 blocked, 7 crawler failures including one robots denial). The three later Chamber additions were processed separately (2 fetched, 1 failed). Thus first-pass totals were 339 processed, 327 fetched/partial, 4 blocked, and 8 crawler failures including one robots denial. Identity `FETCH_FAILED` is 7 because `ROBOTS_DENIED` is reported separately. A repeat run reused all 339 snapshots from cache with **zero new website fetches, failures, or blocks**. Discovery checkpoints likewise made the completed YC routes idempotent on restart.

The run recorded endpoint/page evidence in snapshots but did not persist a raw HTTP request counter. From the saved evidence, the initial crawler made at least 1,265 request calls before any internal retries: 338 non-robots-denied sites each have robots and sitemap checks, plus 589 saved page rows. Actual network attempts may be higher due to bounded retry attempts and redirects. The YC adapter made at least 18 category/robots requests plus accepted detail-page requests; detail attempts beyond the accepted count were not instrumented. These lower bounds are reported explicitly rather than represented as exact totals.

## Existing qualification results

Only the 303 `VERIFIED` additions were passed to the unchanged Phase 3D qualification function and `config/qualification.yaml`. All 36 unverified or unresolved additions retain `NEEDS_REVIEW`/`UNKNOWN` machine defaults. No size thresholds or priority weights changed.

| Dimension | YC additions | Chamber additions | Total |
| --- | ---: | ---: | ---: |
| Eligibility `ELIGIBLE` | 277 | 0 | 277 |
| Eligibility `NEEDS_REVIEW` | 59 | 3 | 62 |
| Eligibility `INELIGIBLE` | 0 | 0 | 0 |
| GEO `HIGH` | 185 | 0 | 185 |
| GEO `MEDIUM` | 63 | 0 | 63 |
| GEO `LOW` | 29 | 0 | 29 |
| GEO `UNKNOWN` | 59 | 3 | 62 |
| Research priority `HIGH` | 185 | 0 | 185 |
| Research priority `MEDIUM` | 63 | 0 | 63 |
| Research priority `LOW` | 29 | 0 | 29 |
| Research priority `NEEDS_REVIEW` | 59 | 3 | 62 |

Nine additions have no employee-size evidence: six YC records and all three Chamber records. Other YC values remain marked directory-reported. No employee-size evidence was invented. All 537 companies in the experimental database are currently `PENDING` for human review; the additions alone create a 339-record review queue. Outreach readiness is exported separately from research priority and remains channel/review state, not a qualification input.

## Bounded contact sample

After qualification, the workflow selected the ten highest-scoring verified `HIGH`-priority additions (IDs 199, 203–205, 207–212). It consumed saved website evidence; Crawl4AI fallback was disabled. Nine companies had contact-channel results and one did not; 16 contact records were created. No other new companies were contact-enriched. A repeat run created zero additional contacts and recognized existing records as duplicates. No messages were generated or sent and no forms were submitted. Contact availability did not affect company priority.

## Comparison with Phase 4E

| Metric | Phase 4E additions (121) | Phase 4F additions (339) |
| --- | ---: | ---: |
| Website identities verified | 97 (80.2%) | 303 (89.4%) |
| Access blocked | 5 (4.1%) | 4 (1.2%) |
| Fetch failed (robots denial reported separately) | 4 (3.3%) | 7 (2.1%) |
| Eligibility `ELIGIBLE` | 76 (62.8%) | 277 (81.7%) |
| GEO opportunity `HIGH` | 50 (41.3%) | 185 (54.6%) |
| Missing size evidence | 12 (9.9%) | 9 (2.7%) |
| Contact result in bounded 10-company sample | 4/10 | 9/10 |

These are different source/category cohorts, not a controlled test of a changed qualification rule. Phase 4F's additions were mostly YC categories selected for breadth and all GEO scores still come from the same existing rules. The apparent yield change should not be interpreted as evidence of a causal improvement or source-wide quality.

## Preservation, reproducibility and verification

The Phase 4F database was initially byte-identical to the Phase 4D experimental copy (`e41871d286b8af764b0ccd7bb390c5c693aa6fb5d13f4431bcaf4c3d193995ad`). A row comparison confirms all 198 original company rows, 201 original provenance rows, and 351 original website snapshots are unchanged in the Phase 4F copy. The source Phase 4D DB hash is unchanged. The frozen 77-company DB SHA-256 is still `b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6`, and the frozen Phase 3D review CSV SHA-256 is `b9c03fe99828b1cfd58501f764665e0a80f009c20f2d234e6e345b08e9364f2f`.

The first discovery sweep took 548.61 seconds; first-pass website processing plus the three Chamber records took about 1,367 seconds; combined live expansion and processing was about **32 minutes**. Reprocessing all 339 new records completed in under one second and reported 339 cache hits, 0 fetches, and 0 failures. Repeated imports added no companies or provenance, and repeated exports were byte-stable. Full test suite: **120 passed**. `git diff --check`: clean.

The repeated export SHA-256 values were: cohort `61a4b626c9abd2863367b83af208616918dc3171eeb789ea2b4b814b47d2ece3`; website verification `a17d84f0c170b1ef670526088219313e5d2d3ee6f739f7b8f4a26eb1fb043f0f`; qualification `e5631b00b5b55f5458cece411cf987f340fec372eeeff1be58bf381d886d4f32`; review queue `9f7dfb848979f6b6563ac5ca57d9f2ac4c32b52ed717854f5cfbe6c463f67369`.

## Reproduction

Make the isolated copy once; do not overwrite an existing Phase 4F database:

```sh
test ! -e data/phase4f_experimental.db && cp -p data/phase4d_experimental.db data/phase4f_experimental.db
```

Run the deterministic YC category sweep and the three bounded Chamber category routes (completed routes are checkpointed):

```sh
OUTREACH_DATABASE_URL=sqlite:///data/phase4f_experimental.db PYTHONPATH=src uv run python scripts/phase4f_scale_cohort.py discover --target 500 --category-limit 50 --delay-seconds 1
OUTREACH_DATABASE_URL=sqlite:///data/phase4f_experimental.db PYTHONPATH=src uv run python scripts/phase4f_scale_cohort.py discover-chamber --category-limit 60 --delay-seconds 1
```

Resume/replay website verification and qualification in batches of at most 25, with no more than ten verified HIGH-priority additions contact-enriched:

```sh
OUTREACH_DATABASE_URL=sqlite:///data/phase4f_experimental.db PYTHONPATH=src uv run python scripts/phase4f_scale_cohort.py process --batch-size 25 --contact-limit 10
```

Add `--no-contacts` to skip contact discovery. The workflow writes `data/phase4f_expanded_cohort.csv`, `data/phase4f_website_verification.csv`, `data/phase4f_qualification_results.csv`, and `data/phase4f_review_queue.csv`; discovery checkpoints and runtime metrics are in `data/phase4f_discovery_checkpoint.json` and `data/phase4f_processing_metrics.json`.
