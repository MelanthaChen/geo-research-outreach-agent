# Phase 4C.2: Wikidata Discovery Quality Analysis

## Decision

**DO_NOT_USE_FOR_AUTOMATED_SMB_DISCOVERY.** The saved 30-record cohort is a useful manually reviewed lead/evidence source, but not an SMB discovery source: only 5/30 website identities remain verified after a conservative false-positive correction, none has employee evidence that supports SMB classification under an evidence-based rule, 24/30 have no P1128 employee count, and all 30 still require human review for scope, operating status, size, or identity. Do not expand the canonical database.

No optional live comparison was run. The offline filter comparison showed no defensible discovery filter likely to improve SMB yield: P31 company was already the input criterion; industry presence modestly improved website verification proportion (25% vs 16.7%) but retained a small, non-random cohort and did not identify any SMB; employee evidence filtering would discard 24 unknown-size entities while retaining known large entities; and inception/country/P576 presence are not reliable substitutes for operating status or size. Another WDQS query would add volume without testing a sound improvement hypothesis.

## Method and reproducibility

This is an offline analysis of `data/phase4c1_wikidata_pilot.csv`, `data/phase4c1_website_verification.csv`, and the saved WDQS/entity JSON snapshots in `data/phase4c1_wikidata_snapshots/`. It makes no network requests and does not change the original Phase 4C.1 evidence. Rebuild the two Phase 4C.2 CSVs with:

```sh
PYTHONPATH=src uv run python scripts/phase4c2_wikidata_quality.py
```

The quality audit retains each original status, captured HTTP status, final URL, title, employee values, and Wikidata claims needed for the interpretation. The diagnostic taxonomy separates `VERIFIED`, `AMBIGUOUS_IDENTITY`, `INSUFFICIENT_EVIDENCE`, `FETCH_FAILED`, `ACCESS_BLOCKED`, and `REJECTED_MISMATCH`. Ambiguous entries are never automatically promoted. Timeouts, DNS/connection errors, response caps, 404s, and 403s are not treated as identity mismatches. The only rejection is a saved redirect from the Julius Pintsch URL to an unrelated gambling site, supported by the foreign host and title. `Deep` was downgraded because a generic one-word entity label collided with “DEEP & DEEP” on `deep2001.com`; the domain's extra digits and title context do not establish that Q950734 owns that site.

The Phase 4C.1 website signal/GEO score describes fetched content, not company suitability. The Phase 4C.2 audit marks scores from non-verified sites as unattributable to that Wikidata company. Identity verification, commercial relevance, GEO website opportunity, and company size remain separate. No Phase 4C.1 status or human decision was overwritten.

## Cohort results

| Measure | Result |
| --- | ---: |
| Companies / unique QIDs / unique normalized domains | 30 / 30 / 30 |
| Original verification: VERIFIED | 7 (23.3%) |
| Conservative diagnostic: VERIFIED | 5 (16.7%) |
| AMBIGUOUS_IDENTITY | 7 (23.3%) |
| INSUFFICIENT_EVIDENCE | 6 (20.0%) |
| FETCH_FAILED | 7 (23.3%) |
| ACCESS_BLOCKED | 4 (13.3%) |
| REJECTED_MISMATCH | 1 (3.3%) |
| Fetch failures | 7/30 (23.3%) |
| Access blocked (403) | 4/30 (13.3%) |
| Missing employee-count evidence | 24/30 (80.0%) |
| Explicit employee-count claims | 6/30 (20.0%) |
| SMB_SUPPORTED by sourced evidence | 0/30 (0.0%) |
| LARGE_ENTERPRISE_SUPPORTED (P1128 >=10,000) | 3/30 (10.0%) |
| SIZE_UNKNOWN | 27/30 (90.0%) |
| Commercial status unconfirmed | 28/30 (93.3%) |
| Noncommercial/foundation indicated | 1/30 (3.3%) |
| Association/trade body indicated | 1/30 (3.3%) |
| Explicit dissolution claim (P576) | 3/30 (10.0%) |
| Human review required | 30/30 (100%) |

Size handling is deliberately conservative and has no invented SMB cutoff. The three sourced counts of 10,000 or more (Intel, Google, CBS Corporation) support large-enterprise classification. Values 400 (MySQL), 1,500 (Burgeranch), and 1,411/1,475 (Orlen Lietuva) remain `SIZE_UNKNOWN` here: without a project-approved/source-backed size framework, these figures are evidence to review, not an arbitrary SMB/large threshold. P1128 values may be time-qualified; consult raw entity claims before interpreting historical counts. Thus the 24 with no count are missing evidence, not small companies.

The original five “potential SMB candidates” used a >=1,000 size split and are not defensible as SMB candidates. The revised supported SMB candidate rate is zero; there may be leads worth human review, but this audit does not call them SMBs. The three dissolution claims are Julius Pintsch AG (1953), Fiat Industrial (2013), and CBS Corporation (2019). No dissolution claim does not prove current operation.

## Website outcomes and relevance

- The seven fetch failures include DNS failures (CanalSat Suisse, beBIR), connection resets (Tianjin Research Institute, Orlen Lietuva), HTTP 404 (Solek URL), timeout (Fiat Industrial), and the configured response-size cap (abc SA). These are access/retrieval limitations, not evidence of a wrong official-site claim.
- Four websites returned 403 (MySQL, Telia Danmark, Stone Island, Burgeranch). No bypass was attempted; all remain `ACCESS_BLOCKED`.
- Seven captures are reachable but identity remains ambiguous (including Chinese/Japanese localized names, brand/legal-name variants, a locale redirect, and a CBS-to-Paramount successor redirect). These were not promoted.
- Six records are insufficient on saved evidence: generic/missing labels (Winston Battery's QID placeholder and FIVOSZ's QID placeholder), blank Safilo title, generic “Home” for Birra Venezia, and the Deep title/domain collision, plus a generic title/non-distinctive label case. See the audit CSV for exact row-level reasons.
- Autismus-Stiftung's identity is verified, but its name indicates a foundation, so it is not counted as a commercial GEO target. AMPROFON's name indicates an association/trade body; commercial status is unconfirmed. The Wikidata `company` class alone does not establish current commercial operation.
- Among verified pages, the captured GEO-content assessments were one MEDIUM, two LOW, one UNKNOWN, and one HIGH from Deep that is no longer attributable after the identity downgrade. These sparse content observations do not establish the companies' research relevance. Unverified page scores are explicitly marked unattributable in the audit.

Additional evidence that could materially improve decisions includes a first-party legal/about/imprint page naming the entity, authoritative QID/legal-entity cross-link, employee count with observation date and source, current operating/dissolution evidence, and industry evidence detailed enough to assess GEO scope. Access-blocked sites need a permitted alternate first-party source, not a control bypass.

## Filter assessment

`data/phase4c2_filter_comparison.csv` gives counts and verified yield for each filter. These are descriptive results on a QID-ordered 30-row cohort, not causal estimates or population performance.

| Filter | Retained | Verified among retained | Assessment |
| --- | ---: | ---: | --- |
| P31 company | 30 | 5 (16.7%) | Already applied; no incremental selection |
| P452 industry present | 20 | 5 (25.0%) | Better metadata completeness; no SMB supported; small, selected sample |
| P571 inception present | 25 | 5 (20.0%) | Does not improve identity/size; age is not size |
| P17 country present | 26 | 5 (19.2%) | Slightly higher coverage; exclude none on missing geography |
| P1128 count present | 6 | 1 (16.7%) | Removes 24 unknown-size entities and still includes enterprise counts; do not use as an SMB gate |
| No P576 dissolution claim | 27 | 5 (18.5%) | Missing P576 does not establish operating status |
| P856 website claim | 30 | 5 (16.7%) | Already universal; claim is not verified identity |
| Identity verified | 5 | 5 (100%) | Appropriate downstream identity gate, not a discovery filter; all five still need size/business review |

`SMB_SUPPORTED` remains zero for every filter subset. No hard exclusions based on missing industry, country, employee count, inception date, or dissolution status are recommended; preserve unknowns and review them.

## Source recommendation

**DO_NOT_USE_FOR_AUTOMATED_SMB_DISCOVERY.** The source can still be consulted as a secondary manual lead source where its CC0 claim-level provenance is useful, but require separate identity, commercial-operation, size, and research-fit validation before consideration. Do not claim 5,000-SMB scale: this sample is small, QID-order-biased, metadata incomplete, and every row needs human review. A better next discovery approach should start from a source with explicit, dated size/operating signals and reliable first-party website fields (for example, a properly licensed structured company directory or official registry with public website mapping), then retain human identity verification. Do not import these records or run contact enrichment.

## Verification

The script uses only local CSV/JSON snapshots. Full-suite tests and reproducibility checks are recorded in the completion summary; repeated export hashes should be identical. The frozen 77-company database and human review fields were not touched.
