# Phase 6A source inventory and permission review

Assessment date: 2026-10-09. This is a conservative operational screening, not legal advice. Public visibility and robots rules are not treated as permission for bulk reuse.

## Decision

No new automated bulk discovery source is cleared for direct company ingestion at this time. Phase 6A therefore made **zero live discovery or company-site requests**. Existing Phase 4F records remain historical records in a copied experimental database; this audit does not validate the rights basis of past collection. Obtain written permission for YC and Greater Dalton before expanding those sources. Do not run their current adapters until that is resolved.

YC’s official [Terms of Use](https://www.ycombinator.com/legal) say that, except where expressly authorized, users must not scrape or use data-mining, robots, scraping, or similar data-gathering/extraction methods (section visible in the current terms). The existing static-category adapter was previously observed using robots-allowed paths, but that does not satisfy the separate express-authorization language. More categories or repeat requests are not a safe route to the 4,463-company gap.

The official [Greater Dalton member directory](https://business.daltonchamber.org/list/) exposes public member listings and the Phase 4D/4F audits checked robots behavior. No explicit automated reuse grant or directory terms approval was established. The chamber-specific terms/permission status is unresolved, so no further bulk crawl is authorized by this workflow. Request written permission from the Chamber/GrowthZone.

## Other source screening

| Source | Permission/access observation | Fit for direct GEO cohort | Decision |
|---|---|---|---|
| [GLEIF Global LEI Index](https://www.gleif.org/en/about/open-data/) | GLEIF says website data is CC0; [bulk concatenated LEI files](https://www.gleif.org/en/lei-data/gleif-concatenated-file/download-the-concatenated-file) are officially downloadable. Current published level-1 file is about 3.46M legal-entity records. | Provides legal name, legal/headquarters address, legal form, status and identifier; not an official company website, company-size signal, SMB indicator, or GEO relevance. LEI population has strong regulated/financial-entity selection bias. Bulk matching to websites would require a separate permitted identity-matching design and could introduce false matches. | Permission is documented, but do not ingest as direct company cohort. Potential separate offline matching pilot requires research approval and validation first. |
| [Illinois Secretary of State business data](https://www.ilsos.gov/data/bus-serv-home.html) | Official page expressly prohibits automated queries to its website. Bulk datasets are listed but their precise reuse grant was not resolved in this audit. | State registration is not evidence of operating business, GEO relevance, official website or SMB status. | Exclude automated querying; do not download/use bulk data until its terms are confirmed. |
| [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | Official API and bulk archives are available; automated use must follow SEC developer/privacy/security guidance. | Public issuer/filer skew; poor proxy for broad SMB discovery, websites, or research suitability. | Exclude from SMB discovery; potentially useful only for public-company enrichment. |
| OpenCorporates | API terms/access tiers apply; no unrestricted bulk entitlement was established for this project. | Registry aggregation can supply legal records, not necessarily first-party websites or GEO fit. | Exclude pending a project-appropriate license or access approval. |

## Current sources and actionable next steps

The machine-readable inventory is [data/phase6a_source_inventory.csv](../data/phase6a_source_inventory.csv). It records official URL, publisher, permission assessment, acquisition method, restrictions, timestamp, category, geography, expected yield and state. The expected new-company yield is deliberately “not estimated” where no permitted candidate universe is known.

1. Request explicit written authorization from YC for academic research extraction from specified static pages and ask for an official export/API if available.
2. Request explicit written directory-data reuse and reasonable access limits from the Greater Dalton Chamber/GrowthZone.
3. Ask the professor/research office whether a licensed company dataset or official open-data source can be supplied. Record license, allowed purpose, fields, retention and redistribution restrictions before adapter work.
4. Consider a separately approved GLEIF entity-to-company identity-match pilot only if it can establish first-party domains from independent, permitted evidence. Do not equate an LEI registration with an SMB or a GEO candidate.

Until one source is cleared and its fields are suitable, automated ingestion remains fail-closed. There is no honest estimate of remaining runtime or company yield; the cohort gap remains 4,463.
