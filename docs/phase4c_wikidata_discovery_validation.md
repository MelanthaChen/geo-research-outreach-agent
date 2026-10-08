# Phase 4C: Wikidata CC0 Company Discovery & Live Cohort Expansion

## Outcome

> **Superseded by Phase 4C.1:** The corrected resumable workflow completed a 30-record pilot with raw snapshots and website verification. See [phase4c1_wikidata_retrieval_validation.md](phase4c1_wikidata_retrieval_validation.md) for current metrics and deliverables.

Wikidata is an appropriate **licensed source to pilot**, but this run did **not** establish a reproducible, verified company cohort. No Wikidata records were imported and the experiment remains at **77 companies**. This is a deliberate no-expansion decision: the live query service returned one bounded 50-row response, but that response was not preserved as a data artifact; an exact bounded rerun returned HTTP 502. The smaller first query timed out, and no website identity checks were run. The requested pilot CSV is therefore header-only, not a representation of a completed 50-record sample.

The original Phase 3C.1 validation database and 77-company review artifacts were not modified. `data/phase4c_expanded_company_cohort.csv` and `data/phase4c_expanded_company_review.csv` are byte-for-byte copies of the frozen Phase 3D review export; they contain the unchanged 77 and **zero additions**. No separate SQLite copy was made because the source-quality and website-verification gate did not pass. No contact enrichment, outreach, or form submission occurred.

## License and endpoint rules

Wikidata states that structured data is CC0 and has no attribution requirement; it recommends acknowledging Wikidata as the data origin and following its data-access best practices. Those practices call for a good identifiable User-Agent, avoiding bursts, honoring `Retry-After`, and setting a reasonable timeout. WDQS is rate-limited and has lower availability than many Wikimedia services. Sources: [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing), [Wikidata data access](https://www.wikidata.org/wiki/Help:Data_access), and [WDQS external endpoint constraints](https://wikitech.wikimedia.org/wiki/Wikidata_Query_Service/Technical_interactions).

This applies to Wikidata statements, not rights in the external company websites named by P856. The website claim remains a candidate association and needs independent validation before ingestion.

## Live query attempts

All queries used the exact company class `Q4830453` and required P856 (official website), without subclass expansion or broad organization matching. Query ordering was by entity URI. The first query also requested labels inline; it timed out at 40 seconds. The simplified query below succeeded once with 20 rows and once with 50 rows. The result was printed in a bounded terminal call but not persisted. A subsequent run of the reproducibility script for that same 50-row query returned HTTP 502. In keeping with WDQS guidance, I stopped rather than retrying the broad query again.

```sparql
SELECT ?company ?website WHERE {
  ?company wdt:P31 wd:Q4830453;
           wdt:P856 ?website.
}
ORDER BY ?company
LIMIT 50
```

| Attempt | Result |
| --- | --- |
| 75 rows + inline labels | Read timeout at 40 seconds; no usable rows |
| Simplified, 20-row cap | HTTP 200; 20 rows returned, but not saved as a sample artifact |
| Simplified, 50-row cap | HTTP 200; 50 rows returned, but not saved as a sample artifact |
| Reproducibility-script rerun, 50-row cap | HTTP 502; no retry |

The query cap was bounded and the successful 50-row request demonstrates that the endpoint can answer this class of query intermittently. It does not satisfy the required preserved 50–100-record pilot. The QID-ordered prefix is also not a representative sample of companies or the SMB population. The available terminal output showed established, recognizable brands alongside less-known entities and a repeated QID with more than one P856 value, but the full returned row set was not retained; no precise rates are asserted.

The initial exploratory requests used an example placeholder email in the User-Agent because no monitored contact address was configured. The reproducibility script uses the project repository URL as an identifiable agent string and does not repeat that placeholder. Before future automated WDQS use, configure a real monitored contact address in the User-Agent.

## Website, size, and cohort quality

- Records with P856 were selected, but no P856 URL was fetched. Thus verified websites: **0**; accessibility, first-party identity, parked/repurposed status, redirects, and social/directory exclusions were not measured.
- Industry, geography, employee-count coverage, large-enterprise bias, ambiguity, GEO relevance, duplicate normalized domains, and overlap with the original 77 were not calculated because the result set was not preserved for analysis.
- Missing P1128 data must remain unknown, not interpreted as small size. No new employee-count or company-size thresholds were introduced.
- New companies added: **0**. Experimental cohort total: **77** (53 baseline eligible, 24 needing review, 0 ineligible; existing Phase 3D state copied without recalculation). The 35 baseline HIGH-priority result is likewise unchanged, not a Phase 4C recomputation.
- Contact-channel coverage was not run; no claims are made about channel availability.

## Implementation and reproducibility

Added a small deterministic response parser and field normalizer in `src/outreach_agent/wikidata.py`, with unit tests for missing fields, multiple websites per QID, duplicate-QID grouping, domain normalization, simple property values, third-party profile host detection, and bounded `Retry-After` handling. A bounded fetch script is in `scripts/phase4c_wikidata_pilot.py`; it is intended to save the query and sample evidence after a successful live run. Its `--baseline-only` mode writes no-addition exports without network access.

Artifacts:

- `data/phase4c_wikidata_pilot.csv` — header only because no live rows were safely persisted.
- `data/phase4c_wikidata_source_quality.csv` — query outcomes and explicit missing measurements.
- `data/phase4c_expanded_company_cohort.csv` and `data/phase4c_expanded_company_review.csv` — copies of the existing 77-company export, no additions.

Repeat after WDQS recovers:

```sh
PYTHONPATH=src uv run python scripts/phase4c_wikidata_pilot.py
```

This script writes CSV evidence only after a successful, bounded query and metadata batch; it does not import into a database. Do not expand or ingest until the full pilot is saved, domain/identity verification is completed, and results support selecting a better-scoped query than the QID-ordered prefix. If suitable, use a disposable copy of `data/phase3c1_validation_77.db`, preserve the frozen baseline, and compare distinct normalized domains and provenance before/after import.

## Verification

- Initial pre-change suite: 69 passed.
- Final suite: **81 passed**; `git diff --check` passed.
- Live WDQS validation: one successful 20-row query and one successful 50-row query, plus one 75-row timeout and one 50-row HTTP 502 as above.
- Import idempotency and qualification compatibility were not exercised because no adapter/import or database changes were warranted by this incomplete pilot.
