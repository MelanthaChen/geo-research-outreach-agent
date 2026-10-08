# Phase 3D: Unified Company Qualification & Outreach Readiness

## Scope and method

This validation used the exact 77 company IDs in the Phase 3C.1 validation database: 47 from the Greater Dalton Chamber Member Directory and 30 from the Y Combinator Consumer Startup Directory. Re-evaluation and export ran against a working copy of that database; the Phase 3C.1 source database, original Phase 3C reports, human review choices, and contact extraction records were not changed. All 77 existing company review decisions were `PENDING` at the time of export. No live requests, contact enrichment, outreach, form submission, or AI-search visibility checks were run for Phase 3D.

The output is a stable, company-ID-ordered CSV with one row per company. It recomputes machine qualification from stored discovery provenance and website snapshots and joins the already stored Phase 3C.1 contact results. Export does not write qualification results back to the database. Company approval and contact/channel review remain separate.

## Existing qualification logic and audit findings

Before Phase 3D, `evaluate()` used a weighted score with configurable thresholds (55 qualified, 30 review). Its initial signals included `has_website=True` regardless of whether a site had been inspected, and treated missing activity as active (`active` defaulted to true). Known small/startup size earned points; the rules included a large-enterprise penalty. These heuristics could move the legacy lifecycle decision without first-party evidence. The size-derived points also made company size a priority driver, although it was usually unknown and no study exclusion was specified.

Industry normalization is a lightweight substring lookup (for example, “construction” maps to “Real Estate” and broad “service” labels map to “Consumer Services”), so it can misclassify mixed/ambiguous descriptions; it is descriptive only. Geography is retained as source text, without geographic validation or a selection rule. Reported employee counts are bucketed by the existing helper (up to 10, 50, and 250); missing counts remain `UNKNOWN`. A source label such as `startup` is not an employee count. Website reachability/fetch status is its own evidence dimension and is not a test of company legitimacy. GEO relevance and confidence were represented by website signals and page count, not measured customer behavior or search exposure.

Eligibility was inferred from website reachability/content, with `LOW_FIT` assigned to reachable pages under an arbitrary 100-character threshold, and blocked/fetch-failed evidence labeled `UNKNOWN`. GEO opportunity was only computed for `ELIGIBLE` companies, so a selection decision and an opportunity assessment were coupled. Website gap signals (FAQ/structured data) and online-discovery relevance were observable proxies, not evidence of actual AI-search visibility. The earlier priority calculation included SMB/size points and eligibility points. Contactability was already stored separately, but Phase 3D makes the separation an enforced property of scoring.

No default geography, industry, or employee-count exclusions are justified by the current research brief. Company directory provenance and a reachable, meaningful first-party website support `ELIGIBLE`; inaccessible sites or thin/insufficient evidence remain `NEEDS_REVIEW`. `INELIGIBLE` is reserved for an explicit exclusion signal named in the configurable `eligibility.explicit_exclusions` list. The list is empty by default. Missing data never means “too large,” “too small,” inactive, or unsuitable.

## Changes made

- Qualification now has the three requested statuses: `ELIGIBLE`, `INELIGIBLE`, `NEEDS_REVIEW`. Missing activity is not positive evidence, and a configured, explicit exclusion is required for `INELIGIBLE`.
- GEO opportunity is assessed independently from eligibility and contactability using only stored website content/structure. Its explanation describes observed evidence and inferred research opportunity; no actual ChatGPT/AI-search visibility claim is made.
- Priority reuses `HIGH` / `MEDIUM` / `LOW` / `NEEDS_REVIEW`, with absolute, configurable YAML weights and thresholds. Contact availability and employee size contribute no points. A company without a suitable channel can still rank highly.
- The former composite qualification score was removed as a competing scale. The legacy database `qualification_score` field now mirrors the single priority score for compatibility; it is not a separate eligibility score.
- `export-company-selection` produces one row per stable company ID with eligibility/opportunity/priority explanations, evidence confidence, fetch state, primary and alternative channels, evidence URLs, named people, outreach readiness, separate company/channel review state, override reason, and missing fields.
- The existing review importer remains compatible with older `review_status` CSVs and now also accepts the unified `company_review_status`, `channel_review_status`, and a documented same-company channel override. Canonical company data remains read-only to CSV edits.
- `review override-channel COMPANY_ID CONTACT_ID --reason ...` stores a distinct human-selected route and reason without rewriting the machine-selected `primary_channel_id`. Only suitable channels belonging to that same company can be selected. The override is review-pending and never sends anything.
- Database initialization adds nullable override fields only; no company/contact/review data is deleted or rewritten.

## 77-company results

| Dimension | Count |
| --- | ---: |
| Eligible | 53 |
| Ineligible | 0 |
| Needs review for eligibility | 24 |
| GEO opportunity HIGH / MEDIUM / LOW / UNKNOWN | 35 / 11 / 7 / 24 |
| Research priority HIGH / MEDIUM / LOW / NEEDS_REVIEW | 35 / 11 / 7 / 24 |
| Evidence confidence HIGH / MEDIUM / LOW | 45 / 13 / 19 |
| Contactability CHANNEL_AVAILABLE | 15 |
| Contactability NEEDS_CHANNEL_REVIEW | 12 |
| Contactability NO_SUITABLE_CHANNEL | 31 |
| Contactability FETCH_FAILED | 19 |
| Companies with an identified suitable primary candidate (including review-pending external routes) | 25 |
| HIGH-priority companies with a suitable primary candidate | 19 |
| HIGH-priority companies without a suitable primary candidate | 16 |
| Eligible companies with a suitable route queued for human review | 24 |

“Companies ready for human review” means the record has enough affirmative evidence to put an eligible company and an identified suitable route in front of a reviewer. It does not mean the company or channel is approved. All 77 company reviews were pending; all channel candidates remain individually reviewable. Of 25 selected primary candidates, 15 are marked `CHANNEL_AVAILABLE`, 10 are `NEEDS_CHANNEL_REVIEW` (cross-domain email/form cases), and two additional companies have `NEEDS_CHANNEL_REVIEW` without a selected primary route.

The 19 HIGH-priority companies with suitable primary candidates comprise 12 `CHANNEL_AVAILABLE` routes (Vexo, Cyclon, Shire Intelligence, antimattr, ACME Block & Brick, Actikare Responsive in-home care, Adcock Financial Group, AIRL, Inc., Alliant Health Plans, Bradley Wellness Center, Bearden Industrial Supply, and Light Anchor) and 7 `NEEDS_CHANNEL_REVIEW` routes (All Ways Caring HomeCare, Atlantic Bay Mortgage Group, Allchem, Inc., Bandy Heritage Center, Barge Design Solutions, Best Buy Metals, and Blood Assurance). The latter are candidates only; they are not approved.

Of 25 companies with selected primary candidates, 10 routes are marked `NEEDS_CHANNEL_REVIEW` due to cross-domain destinations and 15 are marked `CHANNEL_AVAILABLE`; two additional companies have a review-needed contactability state but no selected primary. Thus the high-priority-without-any-suitable-candidate count is 16 (35 HIGH total minus 19 with a candidate), not 23. The channel decision does not affect research priority.

## Missing information and contradictions

- Employee/company size: 67 rows have no defensible employee-size value. Nine rows have a positive, directory-reported count (1–9). One row contains a directory-reported value of zero; it is left blank in the size column and explicitly flagged for verification, rather than being treated as proof of a micro-company or an exclusion. The source’s generic `startup` label is not substituted for an employee count.
- Industry is missing for 10 companies; geography is missing for 3.
- 19 websites are blocked or failed at the last stored inspection. This is an access/evidence limitation, not proof that a company is inactive or has no GEO opportunity.
- Five additional cases have reachable but insufficient/very thin content for the Phase 3D eligibility rule and therefore remain in `NEEDS_REVIEW`; the CSV identifies the row-level missing evidence.
- 24 companies have unknown GEO opportunity because evidence is insufficient. The 35 HIGH and 11 MEDIUM ratings are inferred from public content structure and discovery relevance, not observed search performance.
- No company was marked `INELIGIBLE`: the current study has no preregistered exclusion requirements, and no explicit exclusion evidence was present. This is not a claim that all 77 have been legally or operationally verified. Directory status can be stale, and independent business-activity checks were not performed in this phase.
- The previously stored `LOW_FIT` outcome for a very short page is not retained as an ineligibility classification; low text alone is insufficient to establish a research exclusion.
- This phase did not perform a new manual precision audit of all primary email/form routes. Cross-domain routes, the Blood Assurance program form, and every unapproved route remain human-review items; false-positive rate is not measured.

## Recommendations before scaling to 5,000 companies

1. Preregister actual inclusion/exclusion criteria (if any) before adding them to `eligibility.explicit_exclusions`; do not retrofit geography, industry, or company-size assumptions into the current cohort.
2. Resolve the 24 missing-evidence cases and the zero employee-count anomaly with source-backed research. Record provenance and reviewer notes rather than filling unknowns by inference.
3. Calibrate absolute priority thresholds on a stratified manual sample across discovery sources and industries. The current 35/11/7 distribution is a transparent first calibration, not a statistically validated ranking.
4. Manually validate a stratified sample of selected channels, especially externally hosted destinations and program-specific forms. Track company and channel decisions separately and retain the override reason.
5. Add bounded request-rate, cache, failure, and data-quality monitoring before a 5,000-company run. Estimate crawl budget and review workload from a representative larger pilot, not linear extrapolation from these 77 records.
6. Keep outreach fully manual and out of scope. No approval, export, or channel override triggers sending or form submission.

## Deliverables and verification

- `data/phase3d_company_review_77.csv` — 77 rows, one per existing company ID.
- `data/phase3d_company_selection_summary.csv` — eligibility, GEO opportunity, priority, confidence, contactability, and evidence-gap counts.
- `src/outreach_agent/selection.py` — deterministic unified record/export logic.
- Updated qualification, review import/export, and README documentation.

The test suite covers missing size/geography/industry, unsupported eligibility, GEO opportunity versus actual visibility, contactability-independent scoring, review preservation, channel overrides, stable CSV export/import, and existing deduplication/idempotency behavior. No tests require live network access.
