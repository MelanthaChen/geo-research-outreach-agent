# Phase 3B integrated live validation

Audit date: 2026-10-07

## Method

The integrated pipeline was rerun on the unchanged 28-company sample in `data/phase3_validation_sample.csv`. The HTTP collector was refreshed first. Its 27 successful collections and one fetch failure were then processed by the single `enrich-contacts` workflow with the optional Crawl4AI fallback enabled. The original Phase 3 audit files were not modified.

The fallback was eligible only when the company was already in the HIGH/MEDIUM command scope, the successful stored snapshot contained a first-party high-value page or leadership signal, and lightweight extraction had no defensible named person. It used only URLs already admitted by the bounded HTTP crawl. The generated rows are in `data/phase3b_validation_results.csv`, fallback outcomes in `data/phase3b_validation_fallback_runs.csv`, and manual named-person judgments in `data/phase3b_validation_manual_named_audit.csv`.

## Comparison

| Measure | Phase 3 corrected baseline | Phase 3B integrated |
|---|---:|---:|
| Companies processed | 28 | 28 |
| Companies with contact evidence | 12 | 16 |
| Named contacts found | 0 | 11 |
| Named-person emails found | 0 | 0 |
| Generic/unassociated mailboxes retained | 12 | 12 |
| Contact-not-found outcomes | 15* | 11 |
| Blocked outcomes | 0 | 0 |
| Fetch failures | 1* | 1 |
| Fallback triggers | N/A | 8 (28.6%) |
| Fallback successes / failures | N/A | 8 / 0 |
| Fallback pages processed | N/A | 11 |
| Named contacts added by fallback | N/A | 4 |

\*The historical report grouped “no reliable outreach contact” rather than persisting the final status split. The corrected baseline database contains the comparable 15 `CONTACT_NOT_FOUND` and one `FAILED` outcomes.

All 11 emitted named people were manually checked against their cited first-party evidence: named-person precision was 11/11 (100%), company-association accuracy was 11/11, normalized-role accuracy was 11/11, and evidence accuracy was 11/11. Three display titles (Chris Miles, Scott Chitwood, and Talli Williams) were less specific than the source, but all normalized correctly to `EXECUTIVE`. All 12 retained email records were observed in the stored first-party evidence (12/12 evidence accuracy), and none was attached to a named person without explicit support.

Full-sample recall is intentionally reported as **N/A**. The frozen Phase 3 manual audit documented examples of missed people but was not an exhaustive person-level ground-truth inventory for all 28 sites, so it does not provide a defensible denominator. False negatives remain: the bounded crawl did not expose every public staff record, and no people were recovered from pages that failed or lacked a qualifying leadership URL.

The final cached validation run spent 2.39 seconds inside fallback extraction across eight companies (0.30 seconds per triggered company, 0.22 seconds per processed fallback page). This understates a cold-browser run; the earlier controlled benchmark remains the better cold-runtime reference at 1.049 seconds per successful company versus 0.249 seconds for HTTP. Selective use avoids paying that cost for 20 of 28 companies.

## Representative regressions

- **Vyra:** Crawl4AI recovered Sulan Zhang and Caleb Pong as co-founders from the first-party About page. The shared/public email evidence was not guessed onto either person.
- **Light Anchor:** Crawl4AI recovered Sangha Park and Chase Kim as co-founders. `founders@lightanchor.ai` remained a separate generic founder mailbox.
- **Honeylove:** no accessibility heading was interpreted as a person. The observed accessibility mailbox remained a low-suitability generic record for review.
- **Alrol:** the refreshed first-party site returned HTTP 500, so the company remained `FAILED`, not `CONTACT_NOT_FOUND`; no stale or unrelated leadership names/titles were merged.

## Interpretation

Phase 3B meaningfully improves named-person coverage without losing the HTTP crawler's email evidence or increasing observed incorrect company/person associations. Lightweight HTML alone recovered seven verified people; selective fallback added four more. Package/browser failures are isolated and auditable, and reruns preserve existing review decisions while adding distinct evidence records.

The implementation is ready for a wider, exhaustively labeled validation set, but not for automated outreach. The next evaluation should establish a person-level ground-truth denominator, measure cold and warm fallback runtime separately, and assess title specificity. Human review remains mandatory.
