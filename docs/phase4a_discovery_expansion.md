# Phase 4A: Scalable Company Discovery Expansion

## Outcome

The Phase 3D cohort remains at 77 unique companies. Phase 4A did not add records or force the requested ~200 target: the additional public directories screened either prohibit automated aggregation or do not publish official company websites, which is a required input to this repository's existing discovery model. No company names, URLs, or qualification evidence were fabricated. No new source adapter was added.

The unchanged 77-company baseline is the preserved Phase 3C.1 validation database at `data/phase3c1_validation_77.db`; Phase 3D's frozen review export remains `data/phase3d_company_review_77.csv`. Existing company IDs, website/contact evidence, and human review fields were not modified by this phase.

## Existing sources

| Source | Current behavior | Bounded expansion | Provenance / resume |
| --- | --- | --- | --- |
| Y Combinator Consumer directory | Reads one static industry page, then individual company pages; no directory pagination. Adapter caps requested companies at 50 and links at 250. Excludes inactive entries and entries reporting >250 people. | Can change the static industry URL to another robots-allowed category, but each invocation still sees only that category page. This gives category breadth, not reliable pagination. | Stores YC company ID and detail URL. Canonical normalized-domain dedup keeps one company row and multiple source records. Re-running the same source identity is idempotent. |
| Greater Dalton Chamber GrowthZone | Reads alphabetic listing routes (`A`–`Z`); no next-page cursor. Adapter caps at 60 and stops when the requested number is emitted. | Can read more letters in one run, up to its 60 cap; entries beyond the first alphabetic portions require larger limits. It represents one regional chamber, not broad geography. | Stores detail URL and derived listing identifier. Domain dedup plus unique `(company_id, source_type, source_identifier)` provenance preserves cross-source references and supports reruns. |

The adapters run sequentially, with configured request delays. An important limitation remains: both directory adapters currently proceed when the robots endpoint is unavailable or non-200 (YC also continues after a request error). This is an existing behavior, not expanded here; source-specific robots handling should fail closed in a separately tested hardening change before wider crawling. `services.ingest()` commits after each complete source but does not isolate a source failure occurring mid-iteration. Do not run multiple sources in one unguarded session until per-source transaction isolation is implemented.

## Additional-source screening

- **Georgia Exporter Directory:** official listings include business websites and could be relevant, but Georgia Department of Economic Development site terms prohibit automated scripts to collect information from or otherwise interact with its service. Excluded.
- **US Business Directory:** terms explicitly prohibit robots/spiders/automatic means to access or use the website, including copying material. Excluded.
- **Better Business Bureau:** terms restrict use and prohibit aggregation/compilation for a service with substantially similar directory functionality without permission. Excluded.
- **City of Los Angeles Listing of All Businesses:** official, public dataset; Data.gov metadata identifies a CC0 license and the Socrata API is available. Live schema inspection found 1.7M rows with legal/DBA names, location, NAICS descriptions and activity dates, but no website/domain field. Therefore it cannot safely produce `CompanyCandidate` records in this pipeline without a separate verified first-party URL resolution process. Kept as a manual/future seed source only; no name-to-domain guessing or search-engine scraping was added.
- **City of Seattle Business License:** official active-business dataset with public CSV/ArcGIS resources, but published metadata/API schema does not establish a clear reuse license or contain a company website field. Not selected.
- **SBA DSBS:** public federal procurement directory is potentially relevant, but access/automation policy and a stable public API were not verified during this phase. Not selected.
- **OpenStreetMap / Overpass:** public and license-governed, but it is a general map database rather than a curated research-company directory; its ODbL obligations and public API rate policy require a separate data/license design. Not selected.

The source screen is deliberately conservative. Public visibility alone is not sufficient permission to automate collection, and a public registry name alone is not evidence for an official company website.

## Cohort and quality status

The preserved existing cohort has 77 unique companies: 30 YC and 47 Greater Dalton Chamber companies (80 discovery provenance rows, because three companies have multiple source records). Existing Phase 3D evaluation found 53 eligible, 24 needing review, and 0 ineligible; GEO opportunity/priority distribution and channel coverage remain documented in `docs/phase3d_company_selection_validation.md` and its exports. This phase did not mutate or requalify that validation database, because it is a frozen artifact and a wider live website/contact pass would require explicit new per-run budgets and could change evidence.

As a result, Phase 4A has no new-company additions, no new-source composition, and no new discovery duplicate rate to report. The baseline does not establish a 200-company cohort. It also does not establish current website availability or contact-channel outcomes beyond the last recorded Phase 3D evidence. Existing `NOT_RUN`/fetch-failure states must not be interpreted as no contact channels.

## Follow-up requirements before expansion

1. Identify a directory/API with explicit automated reuse permission and official website URLs, or approve a separately designed, human-reviewed official-site matching stage for open business registries.
2. Make robots handling fail closed for discovery sources and isolate each source's transaction so completed sources remain committed after a later source fails.
3. Add a source registry and bounded `discover-batch` workflow only after a compliant source is selected; include per-source pages, max-new, retries, timeout, dry-run and resume tests.
4. Use a disposable copy of the 77-company database for new live discovery/qualification/contact experiments. Keep the Phase 3C.1 and Phase 3D artifacts immutable.
5. Expand contact discovery within a declared budget and record `NOT_RUN` separately from `CONTACT_NOT_FOUND`.

No outreach sending, contact-form submission, paid enrichment, social scraping, or GEO-platform integration was run or added.

## Verification

- Baseline `uv run --extra dev pytest -q`: 69 passed before the phase work.
- Phase 4A added no code or data changes; the only intended repository change is this report.
- Controlled source checks were read-only: LA catalog metadata/schema, a tiny two-row query sample, API robots policy, and terms pages for candidate directories. No company database was changed and no website/contact crawl was launched.
