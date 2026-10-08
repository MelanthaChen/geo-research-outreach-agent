# Phase 4B: YC OSS Dataset Integration Evaluation

## Decision

**No-go for bulk integration until the rights to reuse the company data are clarified.** The `yc-oss/api` repository is public and its JSON endpoint is technically accessible, but GitHub's repository metadata reports no license, and the README does not state a license or reuse terms for the company records. Public availability, a permissive license for code (none is declared here), or CORS/HTTP access is not a sufficient basis to copy and retain 6,279 company records in this research database. No bulk snapshot, adapter, or database mutation was made. The original 77-company cohort and the prior Phase 4A.1 pilot artifacts remain untouched.

This is a permission-based stop, not a finding that reuse is prohibited. Resume only after an applicable license/permission is identified for the data itself, with attribution and retention requirements recorded.

## Source and bounded evaluation

Checked 2026-10-08:

- [`yc-oss/api` README](https://github.com/yc-oss/api) describes an **unofficial** API sourced from YC's website Algolia search index. It says the records are for publicly launched companies with pages on YC's site, and the GitHub Actions workflow refreshes the data daily. This is secondary extraction/redistribution, not a YC-published API or independent verification of current company status.
- GitHub's repository API returned `license: null`; the repository root has no `LICENSE` file. The README has no data license, attribution grant, or terms covering the company records. YC's underlying terms and rights in the source data were not established by this evaluation.
- The README documented `companies/all.json`, 6,279 companies, 51 batches, 59 industries and 338 tags at its displayed update (October 8, 2026, 03:39 UTC). Counts are mutable and should be rechecked if permission is obtained.
- The endpoint responded to an HTTP byte-range request. A bounded read requested the first 256 KiB; the CDN returned 1,289,936 bytes (about 58% of the 2,221,069-byte object), so the response was stopped at 20 parsed records. It was not saved locally. This is a partial schema inspection, not a full-corpus audit or representative sample. No second endpoint, company website, or contact page was crawled.
- The first record parsed was CircuitHub. The sampled record shape includes `id`, `name`, `slug`, `website`, `all_locations`, `batch`, `industries`, `industry`, `subindustry`, `tags`, `team_size`, `launched_at`, `stage`, `status`, `one_liner`, and `long_description`, among other fields. A supplied YC website URL is a candidate official domain, but the snapshot alone does not prove the domain is live, controlled by that company, or appropriate for the study.

## Fit against this repository

The current `YCPublicDirectorySource` is a distinct HTML adapter for one YC industry directory page. It reads individual YC company pages, caps a run at 50, and keeps active entries with a reported team size no greater than 250. It is not the `yc-oss` JSON source and does not provide a resumable all-company inventory.

If data permission is established, a separate adapter should preserve the existing 77-company baseline and use a disposable database copy. It should:

1. Fetch the documented JSON endpoint with explicit timeout, response-size cap, stable source version/ETag or `last-modified` provenance, and schema validation.
2. Filter to records with a nonblank company name, valid HTTP(S) website, and acceptable target criteria; treat YC membership, team size, status, and website correctness as source claims, not independently verified facts.
3. Deduplicate by normalized website domain through the existing ingest service and preserve YC record ID, YC profile URL, dataset endpoint, observed timestamp, and raw source values in discovery provenance.
4. Keep the baseline 77 as the original cohort and report additions separately. Do not claim 200 unless at least 123 new unique, qualified records are actually added; do not force-fill from weak candidates.
5. Run the existing qualification model only after bounded website inspection. Any contact-channel extraction should be a separately declared small sample and must never send email, submit forms, or contact companies.

No new companies were added, so retained/new/total counts, dedup yield, qualification outcomes, and contact-channel coverage are **not applicable** in this permission-gated evaluation. No partial 20-record sample was inserted, and no site/contact inspection was run.

## Verification

- `uv run --extra dev pytest -q`: **69 passed** (existing suite; no implementation changes in this phase).
- Read-only source checks: GitHub repository metadata and README; endpoint headers and a bounded byte-range/schema inspection.
- No code, database, or frozen Phase 3/4A outputs were changed by this evaluation. The Phase 4B report and its README link are the only intended Phase 4B edits.

## Required next step

Obtain an explicit data license or written permission covering storage and research use of the redistributed company records (including any attribution, retention, refresh, and onward-distribution conditions). If unavailable, continue using sources with established reuse permission rather than bulk-integrating this dataset.
