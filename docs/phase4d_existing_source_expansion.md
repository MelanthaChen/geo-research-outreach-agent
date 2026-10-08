# Phase 4D: Existing Source Expansion & Discovery Bottleneck Audit

## Outcome

The two existing sources expanded the experimental cohort from **77 to 198 unique companies/domains**: 111 net new YC directory records and 10 net new Greater Dalton Chamber records. This is close to the ~200 target; no count was forced. The frozen 77-company database was copied to `data/phase4d_experimental.db` before discovery and was not modified.

All 121 new records remain `NEEDS_REVIEW`, GEO opportunity `UNKNOWN`, and priority `NEEDS_REVIEW` under the existing qualification rules. No website inspection or contact enrichment was run on new records. The original 77 retain their prior website evidence and review states in the copy. One new Chamber listing points to a LinkedIn profile rather than an official company domain; it is retained for auditability and explicitly flagged in the expanded cohort export.

## Why the prior cohort had 30 YC and 47 Chamber companies

These values were captured sample sizes, not source-wide totals. The frozen database has 30 YC provenance records and 50 Chamber provenance records, which normalize to 30 and 47 unique companies respectively. Thus the Chamber count difference is three repeated-domain listings within that source. The YC adapter/CLI allowed up to 50 results, but the frozen cohort contains only the original Consumer-category sample of 30. The Chamber adapter has a 60-result cap and the original import captured 50 source rows. Neither record proves the sources were exhausted.

The current YC source is the official public HTML startup directory, not the YC OSS mirror. Its adapter visits one static industry page and then the linked company profiles; it filters to active records with a website and does not accept reported team sizes above 250. The detail-page route is sequential, with the configured one-second delay. There is no pagination or resume/cache across detail pages, and CLI runs are capped at 50 accepted candidates. The static category pages provide a practical way to broaden the sample without using YC's query-driven `/companies?...` route, which current `robots.txt` disallows. Current `robots.txt` allows static paths; it returned HTTP 200 during this run. The directory describes more than 5,000 companies invested in overall, but that is not an estimate of active, listed, website-bearing companies accepted by this adapter. See YC's [public directory](https://www.ycombinator.com/companies/), [Consumer category](https://www.ycombinator.com/companies/industry/consumer), [Health-Tech category](https://www.ycombinator.com/companies/industry/health-tech), [Consumer Electronics category](https://www.ycombinator.com/companies/industry/consumer-electronics), and [robots.txt](https://www.ycombinator.com/robots.txt).

The Chamber adapter enumerates alphabetical routes A–Z, parses only listing cards that expose an HTTP(S) website, and stops at its 60-record run cap. It has no pagination cursor and does not crawl the many category routes exposed by the directory. The current Chamber landing page shows a broad category index; a sampled A listing page reported 29 results, and a sampled Advertising & Marketing category page reported 10. These category counts overlap the alphabetical listings and are not added to capacity estimates. Current [directory](https://business.daltonchamber.org/list/) `robots.txt` returned HTTP 200 and permits the public routes. The directory footer links a privacy policy, but no explicit automated-reuse grant or separate terms approval was established; the bounded use here is not a legal opinion or a claim of a data license.

The adapter code now fails closed if a robots check errors or returns non-success, and YC provenance identifies the general public directory rather than incorrectly labeling every category as Consumer. It also stores the category URL in the candidate provenance payload for future runs. Tests cover both robots fail-closed paths and YC route provenance.

## Controlled expansion results

The expansion used an isolated copy and the existing adapters/domain deduplication:

| Source | Baseline records / unique companies | Controlled run | Net new unique | Final source-linked domains |
| --- | ---: | ---: | ---: | ---: |
| YC official directory | 30 / 30 | 121 accepted candidates across three static categories; 10 repeated domains | 111 | 141 |
| Greater Dalton Chamber | 50 / 47 | 60 cards across A–Z; 50 matched existing domains | 10 | 57 |
| Total experimental cohort | 80 provenance rows / 77 companies | 181 candidate observations | 121 | 198 |

YC category counts from the live adapter were Consumer 50, Health-Tech 49, and Consumer Electronics 22. The Consumer route has changed since the original cohort, so these 50 were distinct from the original 30 domains. Consumer Electronics repeated 10 domains already present among the experimental sources. YC generated 111 new unique domains from 121 accepted candidates (8.3% repeated-domain rate). Chamber generated 10 new unique domains from 60 listing candidates (83.3% matched the existing cohort). The three original duplicate Chamber provenance rows remain intact. The expanded database has 201 provenance records and 198 companies; normalized-domain duplicate groups in the company table: zero.

The 121 new records all have a directory-supplied website URL because both adapters require one. That is not equivalent to first-party identity verification: the Chamber parser accepted one `linkedin.com/in` URL, flagged by `website_url_is_third_party_profile=true`. Across the new records, 12 have no reported size value; the remaining 109 YC team-size values are source-reported directory data, not independently verified. The existing size mapping yields MICRO 93, SMALL 14, MEDIUM 2, UNKNOWN 12 among the additions; these labels are descriptive and do not establish eligibility or SMB status.

## Research-suitability result

The existing qualification function was applied in the experimental database without changing thresholds or treating source membership as GEO suitability. Final machine totals are 53 `ELIGIBLE`, 145 `NEEDS_REVIEW`, and 0 `REJECTED`; all 121 new records are `NEEDS_REVIEW`. New-record GEO opportunity and priority remain `UNKNOWN` / `NEEDS_REVIEW`, not `HIGH`. Size values for YC are explicitly source-reported, while Chamber size remains unknown. Chamber membership is not used to infer SMB size, and YC membership alone does not qualify a company.

The adapter's YC filter (`active`, website present, reported size <=250) is a discovery-source rule that predates this phase; it is not a new qualification threshold. Any missing reported size remains unknown. The 198-row expanded cohort and review CSV keep that missingness visible, and the Chamber social-profile URL remains reviewable rather than being silently converted into a company website.

## Capacity and permission findings

See [`data/phase4d_source_capacity_audit.csv`](../data/phase4d_source_capacity_audit.csv) for per-source baseline, live-run, URL, pagination, deduplication, access, and remaining-capacity observations. The experiment establishes a lower bound of 111 additional YC records and 10 additional Chamber domains observed in this run; it does not establish the remaining total. YC category coverage is broad enough to show that 30 was a pilot limit, while the Chamber 60-card cap and unqueried category routes leave additional possible members unmeasured. Because the Chamber category routes may substantially overlap A–Z listings, no speculative total is claimed.

Public visibility and robots permission are recorded as observed access signals, not as a blanket license. YC static routes allowed by current robots rules were used; query-driven routes were not. Chamber public alphabetical routes were allowed by current robots rules. No access controls were bypassed, and no contact details were collected. For future larger Chamber reuse, obtain explicit permission if required by the Chamber or GrowthZone terms; do not infer permission from robots alone.

## Reproducibility and verification

The bounded live commands used the copied database (`OUTREACH_DATABASE_URL=sqlite:///data/phase4d_experimental.db`): Chamber limit 60, then YC limits of 50 on Consumer, Health-Tech, and Consumer Electronics. The YC/Chamber public pages were read sequentially with the existing configured request delay. The frozen source hash was checked before and after.

The `phase4d_finalize.py` replay reads only saved provenance from the experimental database and re-ingests it without network. It reported 0 created companies and 0 new provenance rows (60/60 Chamber and 141/141 YC provenance records already existed). The cohort export asserts the company count equals the CSV rows and that no company-domain duplicates exist. Repeated review/cohort exports were byte-stable. No contact enrichment or outreach actions were run.

Verification: full test suite **100 passed**; `git diff --check` clean. The frozen `data/phase3c1_validation_77.db` SHA-256 remained `b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6`.

## Artifacts

- `data/phase4d_experimental.db`: isolated 198-company experimental database.
- [`data/phase4d_expanded_company_cohort.csv`](../data/phase4d_expanded_company_cohort.csv): all 198 records, including baseline/new origin and the social-profile URL flag.
- [`data/phase4d_expanded_company_review.csv`](../data/phase4d_expanded_company_review.csv): full existing human-review export with qualification evidence.
- [`data/phase4d_source_capacity_audit.csv`](../data/phase4d_source_capacity_audit.csv): source capacity and access audit.

The original database, its 77 company IDs, all existing review decisions, and its evidence snapshots remain untouched. The new companies are experimental and should not be imported into the canonical validation database without individual review.
