# Phase 6E — Candidate Pool Screening and Verification

Run date: 2026-10-15. Phase 6E analyzed the existing 57,664-domain pool. It did not acquire additional records, alter campaign selections, send messages, or make any candidate outreach-ready.

## Integrity and scope

The complete pool contains **57,664 unique domains**: 57,127 OSM candidates plus the existing 537-company cohort. Every OSM record received the new structured-data screen; the 537 existing companies were projected into the Phase 6E working copy using their saved identity, qualification, priority, review, contact, and provenance evidence. They were not recrawled or requalified, and their original records and decisions remain untouched. A separate SQLite backup at `data/phase6e_candidates.db` holds the combined screening view and bounded OSM verification outcomes. The Phase 6D source database was read-only; its SHA-256 was `48096cf6f8fcdcdb5d512dc03847e3bec889664f9d82786f67b05b0c20fd3f0d` before and after the run.

The 537-company database, frozen 77-company database, and campaign sidecar also retained their recorded hashes:

| Database | SHA-256 |
| --- | --- |
| `data/phase6a_experimental.db` (537) | `6da43a77a75f009b16657e739a83e13bcadca1697120beab061eb1f8c3b38a13` |
| `data/phase3c1_validation_77.db` | `b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6` |
| `data/phase5d_campaigns.db` | `f28818edabccc5009e24f70c3beee7979fc325c4097f97653f02f523964e0db9` |

The data remains subject to OSM attribution and ODbL 1.0 obligations documented in Phase 6D. The inherited YC and Greater Dalton records retain the source-use limitations recorded in earlier project audits; Phase 6E did not obtain new reuse permission. Do not treat the combined export as cleared for external redistribution without checking those source terms. One domain is a deduplicated website lead—not a verified, currently operating, independent company.

## Cheap-screen rule and calibration

All 57,664 records were assigned a screening status. The 57,127 OSM records were screened from their stored business category, domain, and existing identity/qualification evidence; no website requests or LLM calls were made for this first pass. The 537 established companies retain their saved decisions, projected without altering their source DB. Retail, craft, ordinary commercial/professional offices, local services, food service, and hospitality were classified `LIKELY_FIT`. Museums/attractions and education/institutional categories were kept `UNCERTAIN`; education domains and obvious government domains also remain uncertain. No candidate was auto-excluded by this screen. Domain suffixes, hosted-platform domains, long domains, repeated names, known franchise brands, and multi-object provenance are flags for review—not proof of an incorrect identity or chain.

A deterministic, stratified **140-record offline desk review** sampled 20 records from each of seven broad category groups. I compared the displayed organization name, domain string, OSM category, and location; this was not a live-site visit and cannot confirm operating status or domain identity. Results: I judged 100/100 sampled `LIKELY_FIT` leads plausibly relevant by category/name, and kept 40/40 institutional/cultural records uncertain. No obvious category-level false inclusion was found in the 100 likely-fit sample. This 100% is a limited, subjective *apparent* screen precision—not validated company precision. Since the rule excludes no records, a false-exclusion rate cannot be estimated from this sample. Manual labels and their limitations are preserved in `data/phase6e_calibration_sample.csv`.

## Pool profile

Across the combined pool, there are **54,631 likely-fit** and **3,033 uncertain** records. Of these, 54,234 likely-fit and 2,893 uncertain are OSM leads; the remaining 397 previously verified/eligible businesses are from the existing 537-company cohort. None are marked verified by OSM tags. Restaurants alone are 14,693 (25.72% of OSM leads); cafes 2,321; fast food 1,819; museums 1,449; hairdressers 1,396; bars 1,315; clothes shops 1,293; dentists 1,257; car dealers 1,214; and auto repair shops 1,148. These data are strongly tilted toward local, in-person businesses and the US Northeast—not a representative sample of SMBs or startups.

The OSM source contributes 57,127 leads. The preserved 537-company roster contains 447 from the Y Combinator Public Startup Directory, 30 from the Y Combinator Consumer Startup Directory, and 60 from the Greater Dalton Chamber Member Directory. The broad OSM business-category counts are in `data/phase6e_source_category_distribution.csv`. Candidate location is present on 41,933 OSM rows (73.4%), but state parsing identifies 38,340 rows in the expected Northeast states: NY 12,005; PA 9,699; MA 4,682; NJ 4,316; CT 2,348; NH 1,825; ME 1,621; VT 1,180; RI 664. Another 18,781 rows have no recognizable state code; six rows parse to other states. Location strings are inconsistent and should not be treated as validated addresses. Raw common city/location strings are in `data/phase6e_geography_distribution.csv`.

Domain/provenance quality flags: 650 use an identified hosted-platform suffix (including Square, Wix, WordPress, Shopify, Weebly, Squarespace, Google Business, and GoDaddy subdomains); 55 domains exceed 40 characters; one contains punycode; 3,703 use `.org`, `.gov`, or `.edu` (not all are noncommercial). There are 5,378 candidates with multiple OSM provenance objects; this is a location/chain review signal, not 5,378 additional companies. Exact repeated normalized names span 1,371 name groups; the screen flags 3,239 candidate rows with repeated names, and a conservative name-pattern heuristic flags 270 as likely chain/franchise. These remain separate from verified identity. OSM supplied no employee-size evidence for its 57,127 leads, so the screen cannot call them SMBs; the existing 537 retain their previously stored size fields without reassessment (469 have a non-empty saved `company_size_value`, and 68 do not).

## Bounded website-verification pilot

The run freshly inspected **45 OSM candidate websites** in the Phase 6E working copy: 20 in an initial company-bounded batch, followed by 5 in a hard-capped batch of 25 HTTP attempts. The 20-site batch used existing crawler controls (robots checks, 12-second timeout, one retry, at most two content pages per site, one-second delay); its 81 actual HTTP attempts were recorded after completion. The capped batch made 21 attempts (17 succeeded, 4 failed) and did not exhaust its 25-attempt limit. `data/phase6e_verification_runs.csv` preserves both runs; the first historical run predates HTTP success/failure counters and those fields are intentionally blank. The website-verification export also contains 537 inherited saved-status rows, for 582 total rows—not 582 new fetches.

Across the 45 inspected leads:

| Result | Count |
| --- | ---: |
| Identity verified | 12 |
| Ambiguous identity | 14 |
| Access blocked | 8 |
| Fetch failed | 6 |
| Insufficient evidence | 3 |
| Robots denied | 2 |

Only the 12 identity-verified sites proceeded to the existing qualification rules: 9 GEO-eligible, 3 needing review; among those, 6 had HIGH and 2 MEDIUM GEO opportunity, with the rest UNKNOWN. All 33 non-verified records are `NEEDS_REVIEW` / `UNKNOWN`; status does not turn access failures into identity mismatches. The pilot found 53 successfully fetched pages and 216,155 visible-text characters across pages, 22 inspections with meaningful text, and 23 with the crawler's online-discovery-relevance signal. A detected “contact page” signal exists for 9 of the 45, but it is not a usable contact channel. No email or contact-form channel was discovered or saved for these records: **0/57,127 have `CONTACT_FOUND`**, all remain `NOT_DISCOVERED`, and none is outreach-ready.

The 20-site historical batch plus the new 20-site and capped 5-site batches used 211 HTTP attempts, 8 retries, and 271.11 seconds in total. A simple sequential extrapolation to the 4,572 still-uninspected OSM rows inside the top-5,000 is about **21,400 requests / 7.6 hours**; to all 57,082 still-uninspected OSM candidates, about **267,700 requests / 95.5 hours**. These are rough linear estimates from only 45 varied sites; access blocks, redirects, and timeouts make actual runtime uncertain. No paid API or LLM cost was incurred or estimated.

## Shortlist and current readiness

The top-5,000 export has exactly 5,000 records, all classified `LIKELY_FIT`; it includes **397 pre-existing verified/eligible companies** and 4,603 OSM leads. Of the OSM rows, 4,572 are `DISCOVERED_UNVERIFIED`; 31 have bounded verification outcomes (12 verified, 6 ambiguous, 7 blocked, 3 fetch-failed, 2 insufficient-evidence, and 1 robots-denied). Nine of those new verified records are GEO-eligible. Across the whole combined shortlist there are 409 VERIFIED, 406 GEO-ELIGIBLE, 165 with saved suitable contact channels, and 241 with no suitable channel in the existing records. The category-balanced shortlist contains 2,240 local/consumer services, 1,789 retail, 397 previously qualified cohort companies, 291 professional/business services, 178 craft/local services, and 105 hospitality leads. OSM category allocation prevents restaurants/local services from consuming the entire new-lead allocation, but does not make it population-representative.

Across the full 57,664 pool: **460 identity-verified** (448 inherited + 12 new OSM), **406 GEO-eligible with verified identity** (397 inherited + 9 new), and **170 with saved suitable first-party contact channels** (all inherited; 0 newly found in OSM). All 57,664 remain pending human review and not outreach-ready. For the OSM layer specifically, only 12/57,127 are identity-verified, 9 GEO-eligible, and 0 have a usable saved contact channel. Existing companies retain their original priority scores; OSM leads use an evidence-led heuristic based on category, verification/eligibility, name duplication, and provenance. The `priority_basis` field labels those different score sources; their numeric scores are not directly comparable. Scores are machine-generated queue values, never approvals. Human review and contact discovery remain separate.

## Exports and reproduction

- `data/phase6e_top_5000_candidates.csv` — exact combined shortlist with source/provenance, distinct priority basis, identity/eligibility/contact status, and domain/chain flags. Unverified is explicit.
- `data/phase6e_verified_candidates.csv` — 460 identity-verified rows (448 inherited + 12 OSM).
- `data/phase6e_geo_eligible_candidates.csv` — 406 verified-identity/GEO-eligible rows (397 inherited + 9 OSM).
- `data/phase6e_contact_discovery_priority.csv` — 241 shortlisted verified/eligible rows with no suitable saved channel; this is a queue, not a contact export.
- `data/phase6e_exclusion_uncertainty_report.csv` — 11,752 rows with uncertainty, repeated-name/multi-location, likely-chain, or domain-pattern flags. No candidate was auto-excluded.
- `data/phase6e_website_verification.csv` — 582 saved/new status rows (537 inherited + 45 freshly inspected OSM), with identity evidence, page/content metrics, and contact-page signal.
- `data/phase6e_calibration_sample.csv`, `data/phase6e_source_category_distribution.csv`, `data/phase6e_geography_distribution.csv`, `data/phase6e_source_audit.json`, and `data/phase6e_verification_runs.csv` — calibration, source profile, and run evidence.

Reproduce with:

```bash
PYTHONPATH=src uv run python scripts/phase6e_screen.py audit
PYTHONPATH=src uv run python scripts/phase6e_screen.py screen
PYTHONPATH=src uv run python scripts/phase6e_screen.py sample --per-category 20
PYTHONPATH=src uv run python scripts/phase6e_screen.py review-sample
PYTHONPATH=src uv run python scripts/phase6e_screen.py verify --budget 20 --request-budget 100
PYTHONPATH=src uv run python scripts/phase6e_screen.py export
```

The verification command is resumable and only operates on the Phase 6E copy. `--budget` limits candidate sites; `--request-budget` hard-caps actual HTTP attempts including redirects/retries. `screen` refreshes the OSM screening plus the saved 537-company projection; it never writes to the source cohort. OSM-derived information should be attributed as required by ODbL. The dashboard Companies page exposes the candidate pool with source/screen/identity filters and exports; new OSM leads remain outside approved campaign selections.

## Limitation and next work

This phase did not verify the full new-lead shortlist. OSM is useful as a broad, licensed lead source, but its category mix, local-location bias, shared-domain chains, hosted storefronts, and only 12 verified identities out of 45 newly inspected require a second research/verification pass before treating OSM leads as companies. The next safe step is staged verification of the 4,572 still-unverified OSM records in the shortlist under the hard request cap, followed by identity review, then separate first-party contact discovery only for verified records. No claim is made that the 5,000-row shortlist contains 5,000 verified companies.
