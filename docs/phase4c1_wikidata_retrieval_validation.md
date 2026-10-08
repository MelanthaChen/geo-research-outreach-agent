# Phase 4C.1: Reliable Wikidata Retrieval and Live Pilot

## Result

Completed a **30-company bounded pilot** using two successful WDQS batches. The Phase 4C.1 run uses the existing discovery/evidence/qualification code rather than changing or replacing the pipeline. No records were imported into SQLite, the frozen 77-company database and review decisions were not changed, and no contacts or outreach were processed.

Wikidata class Q4830453 with P856 was used as a seed. This is a narrow class-based sample but not representative: QID ordering yielded prominent large organizations (including Google and Intel). The pilot demonstrates that retrieval and evidence preservation work; the measured website quality and size coverage do **not** support treating this as an SMB/startup discovery cohort without a better-scoped query or human review.

## Retrieval and persistence

- Query shape: company (P31 Q4830453) with official-website statement (P856), ordered by entity URI and website; batches are limited to 20 rows. The second batch uses a keyset cursor (STR(?company) > last_QID_URI); no OFFSET is used.
- Successful WDQS batches: **2**; failed WDQS queries: **0**; retries used: **0**. Both batches returned successfully on their first request. The bounded implementation allows at most two retries for 429, 502, 503, timeouts, and transient network errors, honors Retry-After, and uses capped exponential backoff. It stops if the retry budget is exhausted.
- Each 2xx response is atomically written before JSON parsing. Raw response bodies and metadata sidecars are in data/phase4c1_wikidata_snapshots/. Sidecars record the complete query parameters, request timestamp, response status/headers, URL, and SHA-256. The batched Wikidata entity-label/claims response is also saved. Existing successful filenames are no-overwrite and are reused on resume.
- Saved WDQS snapshot hashes: 51cfe0cb1f2fea61a44a3f51c1c3a5fa34392295e5616524319b6aab5188f57d and ebc49a369d127b3f055186867a76225c59ea3c327b193dbcdf68c91bac6f007e. Entity metadata snapshot hash: 02c31f8d0bb6362b73f0d57b07a8ed3c8a8418c2d6654d950271febff17d21db.
- The prior Phase 4C exploratory requests included a 75-row timeout and a 50-row 502. In this phase the successfully saved pilot batches are smaller; no failed response replaced a successful snapshot.

## Pilot metrics

| Metric | Result |
| --- | ---: |
| Successful WDQS batches | 2 |
| Failed WDQS requests | 0 |
| Retries | 0 |
| Raw SPARQL binding rows across saved responses | 40 |
| Distinct QIDs retrieved across saved responses | 32 |
| Pilot companies selected/exported | 30 |
| Unique QIDs in pilot export | 30 |
| Unique normalized company domains | 30 |
| Duplicate domains in sample | 0 |
| Verified website identities | 7 |
| Reachable but ambiguous identity | 12 |
| Fetch failures | 7 |
| Blocked by website | 4 |
| Explicit employee-size evidence present | 6 / 30 |
| Employee-size evidence missing | 24 / 30 |
| Descriptively large (P1128 value >= 1,000) | 5 |
| Provisional research candidates for human size review | 5 |
| Companies requiring human review | 30 |
| Qualification-compatible ELIGIBLE | 16 |
| Qualification-compatible NEEDS_REVIEW | 14 |
| INELIGIBLE | 0 |
| GEO opportunity | 10 HIGH, 2 MEDIUM, 4 LOW, 14 UNKNOWN |

The employee-count labels above are descriptive pilot summaries, not new eligibility rules. Missing P1128 values remain unknown and are not treated as small. Five entries have explicit employee counts at or above 1,000; Google and Intel are among the obvious large-enterprise examples. The five provisional research candidates are verified website identities not already flagged as large from available P1128 data; this does **not** establish that they are SMBs, and their missing/insufficient size evidence remains a human-review requirement. All 30 records require some human review: 23 have unresolved website identity/access, while verified records still need size/scope review when counts are missing or indicate a large company. The script assigns no size-based eligibility exclusion or qualification threshold. Website verification is conservative: the existing robots-aware website collector checked at most one page per company, and VERIFIED requires a reachable page title matching the Wikidata label. A reachable title that did not establish identity is AMBIGUOUS; blocked/failing sites remain unverified.

The existing qualification function was evaluated in memory from actual website signals; it did not mutate or recalculate the original 77-company database. The outcome is about compatibility with the existing rules, not confirmation that 16 companies are appropriate SMB research targets.

The final resumptive verification/export invocation took **108.98 seconds** while reusing saved WDQS and entity snapshots; it performed the website checks and wrote the final exports. A subsequent offline replay completed in **0.04 seconds** with no HTTP request and produced byte-identical CSVs. The two WDQS request timestamps are preserved in their sidecars.

## Artifacts and commands

- data/phase4c1_wikidata_pilot.csv: 30 selected QIDs with source QID/URL, P856 URL, normalized domain, industry/geography/employee claims, duplicate-domain flag, website status, provisional research-candidate review flag, and the existing qualification outputs. The two saved WDQS responses contain 40 binding rows and 32 distinct QIDs total; the export is capped at the first 30 in stable QID-URI order.
- data/phase4c1_website_verification.csv: one verification result per company, including HTTP/final URL/title, captured website signals, status, and reason.
- data/phase4c1_wikidata_snapshots/: atomic raw WDQS/entity response JSON plus metadata sidecars.

Live bounded retrieval (also resume-safe by default):

```sh
PYTHONPATH=src uv run python scripts/phase4c_wikidata_pilot.py --target 30 --batch-size 20
```

Explicit resume after an interruption (successful query/entity snapshots are reused):

```sh
PYTHONPATH=src uv run python scripts/phase4c_wikidata_pilot.py --resume --target 30 --batch-size 20
```

Offline replay and stable CSV regeneration (uses saved query/entity and website-verification evidence only):

```sh
PYTHONPATH=src uv run python scripts/phase4c_wikidata_pilot.py --offline --target 30 --batch-size 20
```

## Suitability and next step

The pilot is useful as a retrieval reliability test, not as a production discovery source yet. Only 7/30 websites met the conservative identity check, 12 remained ambiguous, 11 were blocked or failed, 24 companies have no employee-count evidence, and the sample includes large enterprises. Do not expand from this QID prefix or import the current records automatically. To pursue a larger SMB-oriented sample, use a reproducible query segmented by explicit industry/geography/batch criteria or retrieve from a Wikidata dump/replica and locally filter; keep WDQS requests small and preserve the same raw-evidence/verification gates. A future query must still validate company identity and size evidence, not infer SMB status from missing data.

## Verification

- Full suite: **88 passed**.
- git diff --check: passed.
- Online run: 40 raw binding rows, 32 distinct QIDs retrieved, 30 selected pilot records, two successful saved WDQS batches, and 30 first-party website checks.
- Offline replay: completed without network; repeated CSV hashes matched exactly.
- Baseline database: untouched; no SQLite import was attempted.
