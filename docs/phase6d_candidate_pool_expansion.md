# Phase 6D — Candidate Pool Expansion

## Result

Phase 6D acquired **57,127 new unique domain candidates** and expanded the combined pool from 537 to **57,664 unique canonical domains** in an isolated database. It does not alter the active 537-company cohort, the frozen 77-company database, or the campaign sidecar. This distinction matters: OpenStreetMap records identify mapped commercial points of interest and their submitted website tags, but do not prove the business is currently operating, the website belongs to the named company, or that the company meets a size criterion. New records therefore start `DISCOVERED_UNVERIFIED`, with GEO eligibility `UNKNOWN`, contacts `NOT_DISCOVERED`, human review `PENDING`, and outreach readiness `NOT_READY`.

## Starting-data audit

| Measure | Audited value | Interpretation |
|---|---:|---|
| Active Phase 6A companies | 537 | Unique canonical domains in `data/phase6a_experimental.db` |
| Frozen validation companies | 77 | Separate database; preserved |
| Existing staged candidate rows | 0 | No separate, valid pre-import staging set found |
| Clean, permission-cleared unimported backlog | 0 | No trustworthy staged set of thousands was found |
| Historical company-like files / database rows | 5,813 references | Repeated snapshots and source records, not unique candidates |
| Historical distinct domains across project artifacts | 577 | Includes the active 537 plus 40 historical domains not in active cohort |
| Historical 40 domains outside active cohort | 40 | Rejected Wikidata pilot, low-confidence LA Registry matches, and `.example` fixtures; not imported or counted as a valid backlog |

The audit included project data, CSV/JSON outputs, SQLite files, discovery reports, and source registries. Previous source decisions were retained: YC automated bulk reuse lacks express authorization; Greater Dalton Chamber bulk reuse permission remains unresolved; the YC OSS records lack a data license; Wikidata was rejected for automated SMB discovery after a 30-company quality pilot; and GLEIF's CC0 legal-entity file is unsuitable for direct company/website/GEO candidate discovery.

## Source and permission

The source used was Geofabrik's dated US Northeast OpenStreetMap extract, acquired from the official bulk-download endpoint: [Geofabrik US Northeast](https://download.geofabrik.de/north-america/us-northeast.html). OpenStreetMap data is provided under [ODbL 1.0](https://www.openstreetmap.org/copyright). The [OSMF legal FAQ](https://osmfoundation.org/wiki/Licence/Licence_and_Legal_FAQ) describes the attribution and share-alike conditions; [OSMF attribution guidelines](https://osmfoundation.org/wiki/Attribution_Guidelines) require clear OpenStreetMap attribution. The regional extract and each record carry source/license provenance. Public redistribution of the derived database must comply with ODbL share-alike; attribution remains required. The downloaded PBF is stored locally under the Git-ignored `data/raw_sources/` directory, with its SHA-256 in the acquisition report.

Selection is limited to named commercial map features with a plausible HTTP(S) `website` or `contact:website`, using the repository's canonical-domain normalizer. Common social/profile/directory hosts and explicit inactive/disused tags are excluded. Duplicate websites are collapsed by exact canonical domain, never by similar company names; every source OSM element is retained in the provenance table. This is an initial lead filter, not an operating-status or identity assertion.

The dated PBF is 1,804,277,099 bytes, SHA-256 `7a57875e39ab0dd4e16183c2ff5c2bb23b8b1a124b308017f5db440ae8a896c3`. The parser examined 2,968,327 named objects: 109,874 had accepted commercial tags and websites; 57,134 domains remained before active-cohort deduplication. Seven source objects matched existing active domains; 52,740 additional source objects shared domains already represented by another source object; 109,867 OSM element provenance records were preserved for the imported set. The source filter excluded 2,785,459 named objects without an acceptable website, 72,956 website-bearing objects outside the commercial tag filter, and 38 explicitly inactive-tagged objects. The net import is 57,127 candidates. Exact acquisition facts are in `data/phase6d_source_acquisition.csv` and `data/phase6d_import_summary.json`; source decisions and yields are in `data/phase6d_source_registry.csv`.

## Acquisition and checkpoints

The actual combined total after import is 537 + 57,127 = 57,664. The database uses a unique constraint on `canonical_domain` and stable candidate IDs derived from the domain. `data/phase6d_checkpoint_report.csv` reports the actual total and whether each 1,000 / 2,500 / 4,000 / 5,000 checkpoint was reached. All four were reached. Snapshot CSVs are present under `data/phase6d_checkpoints/` and contain exactly the preserved 537 baseline rows plus enough deterministic, real OSM candidate rows to meet each checkpoint.

The import is rerunnable. Reimporting the same extract does not add duplicate domains or duplicate provenance rows. Additional OSM objects sharing a website are attached to `candidate_provenance`, not promoted to separate company candidates.

## Validation state

New candidates are processed with explicit bounded calls to `scripts/phase6d_candidates.py verify-batch --limit N` (`N` is capped at 100; default 25). The batch reuses the existing robots-aware website collector, first-party identity classifier, and `config/qualification.yaml` rules, with a two-page limit. Fetch evidence is stored without contact emails/channels. Failures are retained as `FETCH_FAILED` or `ACCESS_BLOCKED` rather than represented as mismatches, and failed sites do not stop the rest of the batch. Verification is resumable; only `DISCOVERED_UNVERIFIED` rows are selected automatically. No contact enrichment, email, form submission, campaign creation, or outreach is run for the new candidate layer.

Two bounded batches examined 20 candidates in 167.45 seconds total with 109 website requests and 4 retries. Among the 20, 5 identities were verified, 9 remained ambiguous, 1 had insufficient evidence, 3 failed to fetch, 1 was access-blocked, and 1 was denied by robots. None was classified as a mismatch. Twelve of the 20 met existing GEO eligibility, and 8 remained `NEEDS_REVIEW`; 10 had `HIGH` and 2 `MEDIUM` GEO opportunity. The other 57,107 remain uninspected with `UNKNOWN` qualification. All 57,127 new candidates still have no contact discovery, are pending human review, and are not outreach-ready. Actual run metrics are in `data/phase6d_verification_runs.csv`; current identity and GEO status distributions are in `data/phase6d_validation_summary.csv`.

## Exports and dashboard

The four generated CSVs are:

- `data/all_candidates.csv`
- `data/verified_companies.csv`
- `data/geo_eligible_companies.csv`
- `data/contact_candidates.csv`

Each includes provenance and explicit verification / eligibility / contact / review / outreach-readiness status. All 57,664 records have a website URL field; 453 have identity status `VERIFIED`, 418 are GEO-eligible under the existing rules, 170 have a suitable first-party email or contact-form channel (all 170 from the preserved active cohort), and 0 are outreach-ready. The dashboard separately reports 153 companies with a suitable first-party business email; 0 new candidates are email/contact enriched. All 57,127 new rows remain pending human review, as do the original 537 company review records. The legacy cohort is represented in combined exports without changing its source database; source reuse notes refer to the existing source inventory rather than implying new permission. A separate, paginated candidate list on Companies can be searched and filtered by source and identity status. It has no campaign-selection controls; candidates are not mixed into company campaigns.

## Integrity and limitations

No real emails were sent; no web forms were submitted; no approvals were created. The original 77-company database, the Phase 6A 537-company database, and campaign sidecar are checked by SHA-256 before/after. The extract is Northeast-only and therefore geographically biased; OSM coverage, tagging completeness, and current operating status vary. Many records can refer to national chains or local franchises, and website reachability/identity still requires validation. The count must not be described as 5,000 verified SMBs. This phase establishes a large, licensed, provenance-backed *candidate lead pool*, not an approved outreach list or a representative sample of SMBs.

## Reproduction

See the Phase 6D section in README for download, import, verification-batch, status, and export commands. The PBF is not tracked by Git; it can be reacquired from the official Geofabrik source. `data/phase6d_candidates.db` is intentionally isolated and ignored alongside other local working databases.
