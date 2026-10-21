# Phase 7 SMB/SME target reconciliation

Snapshot generated from saved local Phase 7 company/contact evidence and read-only hotel-property data. No discovery, crawling, or database mutation was performed.

## Reconciled counts

- Existing Phase 7 company records: **57,664** (57,664 stable company candidate IDs; 57,664 canonical domains).
- Website identity verified: **20,879**; Phase 7 GEO eligible: **19,006** across all sizes.
- Saved positive SMB size evidence: **451** records; `SIZE_UNKNOWN`: **57,213**; large-enterprise evidence: **0**; conflicting size records: **0**.
- Size-supported SMB candidates with verified identity and a substantive first-party website signal: **370**.
- Of these, existing GEO qualification marks **353** eligible.
- Strict contactable, GEO-eligible SMBs: **141**. Gap to 5,000: **4,859**.
- Strict contactable, non-hotel records with unknown size: **6,823**. These remain candidates for review, not automatic exclusions.
- Hotel-tagged candidates in the company pool: **978**; strict-contactable hotel-tagged records: **207**. They are excluded from the 5,000 SMB count and remain in the separate 3,000-hotel target/workflow.
- Separate hotel property pool: **8,494** properties with websites; **7,447** property records share an exact domain with **1,065** general-business domains, including **0** contactable SMB-candidate records. These are potential domain overlaps only; confirmed cross-cohort company links: **0**. Chain sites can serve many properties, so no records are merged or double-counted on domain alone.

## Company-size policy

`SMB_SUPPORTED` requires a positive, source-reported employee count and a consistent saved `MICRO`, `SMALL`, or `MEDIUM` category from the existing project classification. The reconciliation adds no employee or revenue cutoff. This is an evidence label, not SBA certification. `LARGE_ENTERPRISE_SUPPORTED` uses the existing saved `LARGE` category. Missing, zero, or unsourced values remain `SIZE_UNKNOWN`; inconsistent saved counts/categories are `CONFLICTING_EVIDENCE`. No count or revenue is inferred from sector, website appearance, age, source affiliation, or geography.

## Contact channels

The classifications below count companies, not channel rows. A company with both methods appears once as `EMAIL_AND_FORM`. `NO_CONTACT` means the current strict Phase 7 export has no qualifying public business email or confirmed first-party form; lower-quality, contact-page-only, rejected, or unverified evidence may still exist.

### Full 57,664-record candidate pool

| Channel class | Companies |
|---|---:|
| EMAIL_ONLY | 4,184 |
| FORM_ONLY | 2,132 |
| EMAIL_AND_FORM | 856 |
| NO_CONTACT | 50,492 |

### All 7,172 strict contactable companies

| Channel class | Companies |
|---|---:|
| EMAIL_ONLY | 4,184 |
| FORM_ONLY | 2,132 |
| EMAIL_AND_FORM | 856 |
| NO_CONTACT | 0 |

### SMBs that meet the saved size, identity, GEO, and contact criteria

| Channel class | Companies |
|---|---:|
| EMAIL_ONLY | 131 |
| FORM_ONLY | 4 |
| EMAIL_AND_FORM | 6 |
| NO_CONTACT | 0 |

## Source, sector, and geography

Source composition of the full Phase 7 company records:

| Category | Companies |
|---|---:|
| OpenStreetMap (Geofabrik US Northeast extract) | 57,127 |
| Y Combinator Public Startup Directory | 447 |
| Greater Dalton Chamber Member Directory | 60 |
| Y Combinator Consumer Startup Directory | 30 |

Sector distribution of strict contactable records (raw source categories are retained in the CSV):

| Category | Companies |
|---|---:|
| Retail | 2,785 |
| Food and beverage | 2,479 |
| Other / source category retained | 565 |
| Professional and business services | 408 |
| Manufacturing and trades | 367 |
| Healthcare | 298 |
| Travel and lodging | 253 |
| Unknown | 17 |

Most common saved locations among strict contactable records:

| Category | Companies |
|---|---:|
| UNKNOWN | 1,969 |
| New York, NY | 311 |
| Brooklyn, NY | 128 |
| Philadelphia, PA | 125 |
| Boston, MA | 103 |
| San Francisco | 98 |
| Pittsburgh, PA | 77 |
| Buffalo, NY | 65 |
| Allentown, PA | 49 |
| Cambridge, MA | 47 |
| Montclair, NJ | 42 |
| New York | 38 |
| Lancaster, PA | 35 |
| Rochester, NY | 35 |
| State College, PA | 32 |
| Albany, NY | 31 |
| Portland, ME | 31 |
| Providence, RI | 31 |
| West Chester, PA | 31 |
| Nashua, NH | 30 |
| Somerville, MA | 27 |
| New York, NY, US | 25 |
| Syracuse, NY, US | 25 |
| Gettysburg, PA | 24 |
| Syracuse, NY | 24 |

Potential exact-name collision rows across different candidate IDs: **443**. They remain separate candidate records and are not merged by name; identity review is required before counting any unresolved candidate as a distinct company.

## Interpretation and limitations

The 7,172 strict-contactable total is not an SMB total. The saved Phase 7 schema has no employee-size evidence for the OSM records. The only usable saved size evidence comes from the original company-size fields and is joined by canonical domain to the Phase 7 stable candidate ID. The current evidence supports 370 verified, operating-signal, size-supported SMB candidates and 353 GEO-eligible SMBs; 141 also meet the strict contactability criteria. A further 1 contactable record(s) with reported SMB size have unresolved same-name/location identity collisions. Other contactable businesses stay `SIZE_UNKNOWN` and need sourced size evidence or human review. This does not establish that all unknown-size firms are ineligible.

The operating signal is the existing bounded first-party website check: identity status `VERIFIED` plus a saved successful page with substantive visible text. It is not proof of business registration or current transactions. Counts use stable Phase 7 candidate IDs, not channel rows. Existing company-size evidence is matched to those IDs by canonical domain; exact normalized name/location collisions are flagged and excluded from the count pending identity review, not automatically merged. The Phase 7 source schema enforces one candidate per domain, so unobserved alias-domain identities may still need human reconciliation. The Northeast-heavy OSM pool and its business-category mix are not representative of the broader SMB/SME population.

Contact email/form evidence, evidence excerpts, source URLs, and review states are retained in the contactable-company exports. The reconciliation includes each candidate's primary source URL/record ID/license and the count of source provenance records; the complete provenance table remains intact in the source database. Existing channel-review decisions remain unchanged; the saved contact channels remain pending unless a prior human decision says otherwise. No outreach authorization is created.

## Reproducibility

Run `PYTHONPATH=src uv run python scripts/phase7_smb_reconcile.py` to regenerate the reconciliation CSVs and this report from the saved Phase 7 database, the preserved 537-company database, current hotel-property data, and existing Phase 7 exports. The command opens databases read-only and does not issue website requests.

### Export paths

- `data/phase7_smb_reconciliation.csv` (all Phase 7 candidate records)
- `data/phase7_smb_contactable_companies.csv` (the 7,172 strict-contactable records with SMB status)
- `data/phase7_smb_size_review_queue.csv` (non-hotel strict-contactable candidates with unknown size)
- `data/phase7_cohort_domain_overlap.csv` (unconfirmed exact-domain review leads across separate pools)
- `data/phase7_smb_reconciliation_summary.json`
