# Phase 4A.1: Open Dataset Discovery & Verified Website Matching

## Result

The LA Open Data dataset is a legally reusable source of business-register facts, but not a direct website directory. A reproducible 30-record pilot found **3 verified first-party website matches**, **1 candidate whose site could not be fetched/confirmed**, **4 ambiguous cases**, **1 rejected mismatch**, and **21 records with no first-party website candidate found**. Only 3/30 (10%) met the identity-evidence bar for `VERIFIED`; this is too little, and too skewed in company type, to treat the registry as a standalone ~200-company discovery source. No pilot company was inserted into the canonical company table or any validation database.

The row-level sample, source record identifiers, lookup queries, candidate domains, evidence URLs, explicit status, confidence and review notes are in [`data/phase4a1_website_matching_pilot.csv`](../data/phase4a1_website_matching_pilot.csv). A verified website match establishes identity only; it does not establish study eligibility or SMB/startup fit.

## Dataset verification

Checked on 2026-10-08:

- The [Data.gov record](https://catalog.data.gov/dataset/listing-of-all-businesses) identifies the publisher as City of Los Angeles / data.lacity.org and gives the dataset license as [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
- The underlying Socrata view metadata at [`https://data.lacity.org/api/views/r4uk-afju`](https://data.lacity.org/api/views/r4uk-afju) independently returns `licenseId: CC0_10`, official provenance, and the dataset columns. The resource API at [`https://data.lacity.org/resource/r4uk-afju.json`](https://data.lacity.org/resource/r4uk-afju.json) was queried successfully without authentication. The legacy CSV rows endpoint responded `410 Gone`, so the pilot used the current JSON API rather than the retired endpoint.
- Metadata described 1,706,526 records. A live count query for `location_end_date IS NULL` returned 769,237 records at the time of sampling. This is a date-field filter, not an authoritative certification of current operation.
- The record exposes location account ID, legal name, optional DBA, address, city, ZIP, NAICS/description, location start/end dates and geography fields. A schema-column audit found no company website, URL, domain, phone, or email column.
- The dataset is updated monthly per its description; its live view metadata reported rows updated 2026-09-16. Registry entries may represent individuals, property entities, businesses that operate outside the listed city, multiple business locations, and active/inactive registrations. A null location end date does not independently prove current operation.
- `https://data.lacity.org/robots.txt` was fetched and sets a one-second crawl delay for all user agents. This pilot kept requests sequential with at least 1.05 seconds between requests.

## Pilot sampling method

The sample is **30 active-filtered registry rows**, not 30 verified unique operating companies. Using Python `random.Random(41027)`, 30 distinct integer offsets were sampled from `[0, 769237)`. Each record was fetched using the public API with `location_end_date IS NULL`, stable `location_account` ordering, a one-row limit and the selected offset. The seed, population count, exact record IDs and row contents are preserved in the CSV so a reviewer can inspect every selection.

The API is a mutable live view, not an immutable snapshot. Repeating the same seed against a later or changing view may map offsets to different rows; therefore the preserved source account IDs and sample CSV are the exact frozen sample. This is a simple reproducible pilot, not a statistically powered estimate of website prevalence. Selection by registry row can also oversample entities with multiple registered locations; the sample was not deduplicated to legal parent company because parent/location relationships are not reliably given.

The sample spans household appliance sales, real estate/property entities, production, construction, health services, restaurant, retail, childcare, landscaping, personal services, R&D and film/video. It includes common personal names, LLCs with weak public identity, DBA/trade names, a shared-office address, one large enterprise, and a multi-location business. Size evidence is recorded only where found on a first-party site; no employee-count estimates were inferred from NAICS or legal form.

## Candidate search and verification protocol

For each sampled row, a targeted public web-search query included its legal/DBA name and location or address. Search was performed through the web search tool, not by scraping search-engine result pages. Search-result and public-register pages were discovery/corroboration evidence only; they were not accepted as the company's website. Potential social, marketplace, and third-party directory pages were not treated as official websites. Candidate sites were checked for first-party name and location statements. No opaque model score was used.

Statuses mean:

- `VERIFIED`: a first-party page states a matching business identity, and at least one additional strong signal (address/city/suite or official DBA/trading-name relation) matches.
- `PROBABLE_NEEDS_REVIEW`: there is a credible but incomplete candidate. None of the 30 reached this exact state; weaker candidate cases are instead `AMBIGUOUS` or `FETCH_FAILED`.
- `AMBIGUOUS`: candidates or public records conflict, or multiple same-name entities cannot be resolved safely.
- `NOT_FOUND`: no official company domain surfaced in the bounded query. It does **not** mean that no website exists.
- `REJECTED_MISMATCH`: available public evidence conflicts materially with the sampled record; rejected from this match.
- `FETCH_FAILED`: a candidate was surfaced, but the first-party page could not be fetched/confirmed in this run.

### Verified matches

| Registry record | Candidate | Strong matching evidence | Caveat |
| --- | --- | --- | --- |
| `0002034494-0001-0`, Behavior Frontiers, LLC | [behaviorfrontiers.com](https://www.behaviorfrontiers.com/contact) | First-party contact page identifies the LLC and exact El Segundo street/suite; About page reports workforce size. | The first-party About page reports 4,000+ employees nationally, so this is verified identity but likely outside the SMB/startup target. |
| `0002681574-0001-3`, Goodwin Consulting Group Inc | [goodwinconsultinggroup.net](https://www.goodwinconsultinggroup.net/contact-us/) | First-party contact page gives exact company name and 655 University Avenue, Suite 200; city/ZIP match the record. | The record's address is Sacramento despite the dataset's LA focus; preserve this geography caveat. |
| `0002725790-0001-5`, Culver West LP / Playa Provisions | [playaprovisions.com](https://www.playaprovisions.com/contactinfo) | First-party contact page identifies the Playa Provisions trade name and exact 119 Culver Blvd location. | Identity is verified for this brand/location, not the full legal entity's other operations. |

The two named-company results and one brand/location result demonstrate that matching can work in individual cases. They do not establish an acceptable automatic source yield or selection-quality rate. In particular, Behavior Frontiers would not be a suitable addition to an SMB-focused cohort without clarified inclusion rules.

### Ambiguous, failed, and rejected cases

- `ABOUT-FACE DVIP INC`: a county listing corroborates the name/address; a community listing surfaced `aboutfacedvip.com`, but no first-party identity page was confirmed. Status `FETCH_FAILED`, not verified.
- `FAIRVIEW CONSTRUCTION`: city permit records corroborate a business name, address and phone; a third-party listing associated a candidate domain, but multiple Fairview firms/domains exist and the candidate's first-party identity was not established. `AMBIGUOUS`.
- `BTC BUILDERS INC`: same-name businesses surfaced in more than one jurisdiction; no first-party website tied the candidate to this LA record. `AMBIGUOUS`.
- `MIGUEL NAVARRO LANDSCAPING`: common personal name and conflicting unrelated businesses/search results. `AMBIGUOUS`.
- `NIGEL THOMAS`: multiple production entities were associated with the same office address; the row does not establish which company identity or domain belongs to the record. `AMBIGUOUS`.
- `FLAGSHIP HOLDINGS LLC`: a later public California entity profile showed a Bakersfield address and solar-services description, conflicting with this row's location/industry framing. `REJECTED_MISMATCH`; no website candidate was considered.
- The remaining 21 rows are `NOT_FOUND`. Some have a DBA or exact city address; that is not enough to invent a domain. Several are individual registrations, real-estate entities, storefronts or small service businesses that may operate without a standalone site.

## Pipeline and data isolation

- No code adapter was added: this dataset has no website field and no compliant, reliable automatic official-site resolver was established.
- No registry row or website candidate was ingested into `Company`, and no source provenance was added to the production application database.
- No website crawl/contact extraction, qualification, GEO scoring, outreach, form submission, or external sending occurred.
- The original 77-company validation DB, company IDs, review decisions and evidence were not touched. The CSV under `data/` is an isolated pilot artifact, not an alternate database or parallel pipeline.

## Recommendation

The LA registry is reusable as a **manual or semi-manual research queue**, not as a direct company-discovery adapter. Its broad legal-name/address dataset contains a high share of records that are not clearly target operating companies, and candidate website matching is low-yield and often ambiguous under a conservative identity standard. Do not connect it to automatic ingestion yet.

Before a larger pilot, decide whether inclusion is limited to a target geography (the current source contains out-of-area address values), whether parent entities or individual trade names qualify, and what size/sector requirements are part of the study. Then either use an open directory that already provides first-party domains, or approve a documented human-review step for registry-to-site matching. Any only-`VERIFIED` ingestion should occur later through the existing discovery/dedup/provenance/qualification flow, against a disposable database copy, with the current 77-company baseline left intact.
