# Phase 3C: Business Channel Discovery & Ranking Validation

## Scope and method

This phase adds public business-channel discovery to the existing outreach contact pipeline. It does not submit forms or messages. The collector visits the configured site and a bounded set of likely high-value pages, records explicit first-party evidence, detects contact forms and relevant links, and ranks one primary channel when the evidence supports one. Other discovered channels remain available as alternatives. Support, privacy, accessibility, legal, security, and other unsuitable channels are not recommended.

Two fixed samples were evaluated: the prior 28-company Phase 3 validation set and the 77-company expansion set from the existing qualification database. Website inspection was refreshed once per sample with the configured page limit and request delay. Crawl4AI fallback was enabled for the 77-company enrichment run, capped by the existing per-run company limit. Contact extraction was local and did not submit forms. Results were reprocessed locally after extraction-context fixes; the original source databases were not modified.

## Results

| Measure | 28-company sample | 77-company sample |
| --- | ---: | ---: |
| Website inspection | 27 fetched, 0 blocked, 1 failed | 58 fetched, 12 blocked, 7 failed |
| Contact discovery | 17 found, 10 not found, 0 blocked, 1 failed | 33 found, 25 not found, 12 blocked, 7 failed |
| Companies with primary channel | 6 | 17 |
| `READY_FOR_REVIEW` | 5 | 12 |
| `NEEDS_REVIEW` | 2 (1 has primary) | 7 (5 have primary) |
| `NO_SUITABLE_CHANNEL` | 20 | 39 |
| `FETCH_FAILED` | 1 | 19 |
| Crawl4AI fallback | Not run | 19 runs, 19 succeeded, 0 failed; 27 pages; 5 contacts added, 6 candidates rejected; 7.12s recorded runtime |

The 77-company primary-channel totals by type were: 3 contact forms, 1 business-development email, 12 general-business emails, and 1 sales/marketing email. The channel inventory also retained 9 `OTHER`, 4 restricted, and 3 support channels, none of which are suitable primary recommendations. The 28-company primary totals were 3 contact forms and 3 general-business emails. These are counts of persisted recommendations, not claims of successful contact or deliverability.

Contactability status is intentionally shown separately from primary-channel assignment: two companies in each sample were marked `NEEDS_REVIEW` without a primary channel. This is an implementation consistency gap, not an additional set of recommended channels.

## Spot-checks and calibration

- The 77-company run produced one externally hosted form recommendation: YouArt's Typeform Enterprise destination, linked from YouArt's website. It remains `NEEDS_REVIEW` so a human can verify that the third-party form is an official business channel.
- Forms were detected from actual form markup or explicit contact links; newsletters, login, careers, and restricted-purpose forms are excluded. No form was submitted.
- Restricted accessibility/privacy/support evidence was retained as unsuitable rather than promoted.
- The audit showed that older cached contact evidence could conflate distant page text with the nearby channel. A regression test now exercises this migration path, and JSON-LD email evidence is retained separately from its human-readable purpose. Bare visible addresses are treated conservatively.
- Some of the 12 `GENERAL_BUSINESS_EMAIL` primaries in the 77-company run are named-looking addresses or organization-level records without a clearly explicit business purpose. They are surfaced for review, not automatically interpreted as partnership intent. A further rule to distinguish likely named mailboxes from general business channels would reduce over-broad classification.
- Some older cached rows remain imperfect until a deliberate refresh/rebuild: a few channel records may preserve legacy context or a broad classification. The CSVs preserve source/evidence fields for human review; do not treat confidence labels as independently verified truth.

No statistical precision/recall estimate is claimed: the sample was not randomly drawn and no exhaustive manual gold-label review was completed. The primary-channel counts measure what the bounded pipeline surfaced, not all channels published on those websites. Blocked sites, transient timeouts, JavaScript-only forms, anti-bot behavior, missing sitemaps, and cross-domain branding can reduce recall or require human verification.

## Files and recommendation

- `data/phase3c_validation_28_results.csv`
- `data/phase3c_validation_77_results.csv`

Phase 3C is suitable for a human-reviewed pilot, not unattended outreach. Before scaling, align `contactability_status` with the primary-channel assignment in all edge paths, rebuild legacy rows from raw page markup where available, manually review a stratified sample of `OTHER` and cross-domain results, and track false positives and missed channels against an explicitly reviewed gold set.
