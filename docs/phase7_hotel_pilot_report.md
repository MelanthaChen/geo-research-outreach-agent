# Phase 7 hotel discovery and bounded pilot

Snapshot: 2026-10-21. Hotel properties are stored in the separate, ignored local database `data/hotel_candidates.db`; they are not mixed into the 537-company outreach cohort or counted toward the SMB/SME target.

## Source acquisition

Official Geofabrik regional OpenStreetMap extracts were downloaded and parsed locally. OSM source records retain the stable element ID and element URL. Source dataset URL and file hash are stored in `hotel_provenance` and `import_runs`. The source is OpenStreetMap data under ODbL 1.0; OSM attribution and share-alike obligations apply to database distribution. These are property candidates, not verified businesses. OSM tags do not prove current operation, independent ownership, or suitability.

| Extract | Named hotel properties added | With website URL | No website URL | Third-party website excluded |
|---|---:|---:|---:|---:|
| US Northeast (previously imported) | 5,475 | 3,208 | 2,267 | 7 |
| California | 4,425 | 2,202 | 2,223 | 1 |
| Texas | 3,876 | 2,212 | 1,664 | 2 |
| Georgia | 1,486 | 872 | 614 | 2 |
| Total | 15,262 | 8,494 | 6,768 | 12 |

Of the 8,494 tagged website records, 12 were known social, directory, or booking domains and excluded from automatic first-party website inspection. The remaining 8,482 website candidates span 2,026 canonical domains. Chain-indicated records are preserved as separate properties: 9,783 records have a brand/operator signal. Property-level IDs deduplicate overlapping source extracts without collapsing locations that share a chain domain.

The state extracts were obtained from [Geofabrik's US download index](https://download.geofabrik.de/north-america/us.html), with specific [California](https://download.geofabrik.de/north-america/us/california.html), [Texas](https://download.geofabrik.de/north-america/us/texas.html), and [Georgia](https://download.geofabrik.de/north-america/us/georgia.html) pages. OpenStreetMap's license FAQ describes ODbL as attribution/share-alike for data and databases; see the [OpenStreetMap Foundation license FAQ](https://osmfoundation.org/wiki/Licence/Licence_and_Legal_FAQ). Extract acquisition avoids a high-frequency Overpass query workload; the [Overpass API guidance](https://wiki.openstreetmap.org/wiki/Overpass_API) recommends extracts/caching for recurring use and respecting service limits.

The official [Florida extract page](https://download.geofabrik.de/north-america/us/florida.html) was evaluated as a possible supplemental source. The PBF transfer returned no bytes after 63 seconds and a bounded 20-second HEAD request also timed out, so it was aborted and no Florida records were imported. Do not count it in the source totals above.

## Pilot method and measured result

The pilot selected 25 never-before-processed properties deterministically, with at most one property per canonical domain, alternating chain-indicated and independent/unknown properties, ordered by location and stable OSM ID. It reused the existing website identity and GEO qualification logic, checked robots/access status, used a 15-second request timeout, one bounded retry, no sitemap crawl, concurrency one, and a 100-request budget. No CAPTCHA or access control was bypassed.

Measured pilot results:

- 25 candidates processed; 74 HTTP attempts; 58.55 seconds wall time.
- 11 identity-verified (44%); 10 GEO-eligible (40% of the sample); all other records remain explicitly unresolved/failed/blocked or ineligible.
- 7 GEO-eligible properties had public first-party business emails; 4 had confirmed first-party contact-form evidence; 2 had both; 9 had at least one of those channels. All new channels remain `PENDING` human review.
- 5 access blocked, 5 ambiguous identity, 2 robots denied, 1 fetch failed, 1 rejected mismatch. Blocked/fetch failures are not labeled identity mismatches.
- 1 property had an operational booking signal that still requires human confirmation. The pilot does not claim 10 operating hotels as human-verified.
- 23 sitemap checks were skipped by design. Recorded mean HTTP latency was 325.69 ms, p95 591.35 ms; request throttling and per-domain spacing make end-to-end time larger than raw network latency.

At worker launch: 15,262 property candidates; 8,457 website candidates pending first inspection; 11 identity verified; 10 GEO eligible; 9 with usable contact evidence; 1 operating signal awaiting human confirmation. After three production batches, the checkpoint showed 100 actually inspected properties including the 25-property pilot, 30 identity verified, 27 GEO eligible, 16 with usable contact evidence, and 8,382 pending first inspection. After the quality pause, two batches using a domain-diverse independent/unknown and chain-indicated round-robin selection each yielded 8 verified identities and 8 GEO-eligible properties. The first used 74 HTTP attempts and had 3/25 access blocks (12%); the second used 73 HTTP attempts and had 5/25 blocks (20%). This is an early observed result, not enough evidence for a sustained yield claim. The next batch was in progress at the latest read: the database showed 152 actually inspected, 46 identity verified, 42 GEO-eligible, 24 with contact evidence (16 email, 11 forms, 3 both), and 8,330 pending. The latest batch-boundary exports contain 46 verified properties, 42 GEO-eligible properties/profile records, and 38 contact-channel rows. The current live totals include two partial inspections in the active batch. None is outreach-approved.

## Independent worker

The macOS LaunchAgent `com.geo-research-outreach-agent.hotels` is loaded and running (latest observed PID `94476`). It uses a separate lock (`/tmp/geo-research-hotel-pipeline.lock`), database, and stop flag from the Phase 7 SMB/business worker. It has finite cumulative caps of 25,002 HTTP attempts and 172,802 seconds (48 hours), 25-property batches, and concurrency one. The first three production batches used 212 HTTP attempts in 200.37 seconds and added 19 verified identities and 17 GEO-eligible properties. Their access-block outcomes were 12/25, 8/25, and 13/25, compared with 5/25 in the bounded pilot. The third batch crossed the 40% full-batch quality pause (13/25); the checkpoint was preserved. The following two domain-diverse batches each yielded 8 verified identities and 8 GEO-eligible properties, with 3/25 then 5/25 access blocked (74 and 73 HTTP attempts). Two batches do not establish a sustained yield improvement; the 40% circuit breaker remains active. Candidate rows are persisted property by property; exports refresh after each complete batch. `KeepAlive` is disabled to prevent an unbounded relaunch after a cap or failure; `caffeinate` holds the Mac awake only while this finite job is active.

```bash
PYTHONPATH=src uv run python scripts/phase7_hotels.py status
PYTHONPATH=src uv run python scripts/phase7_hotels.py stop
PYTHONPATH=src uv run python scripts/phase7_hotels.py resume --request-increment 20000 --time-increment-seconds 86400
```

Logs: `logs/hotel_worker.log` and `logs/hotel_worker.err.log`. The worker refreshes property and profile exports after each batch. A later `resume` only raises explicit request/time caps; it does not reset previous candidate results. Processed-property metrics are based on actual inspection timestamps, not missing-website rows. If an individual full batch has at least 40% access blocks, it stops at the boundary and records `quality_degradation_pause_access_block_rate` for review.

## Exports and GEO handoff

- `data/hotel_candidates.csv`
- `data/hotel_verified_properties.csv`
- `data/hotel_geo_eligible_properties.csv`
- `data/hotel_contact_candidates.csv`
- `data/hotel_validation_failures.csv`
- `data/geo_engine_hotel_profiles.jsonl` (10 records at launch)
- `data/geo_engine_startup_profiles.jsonl` (376 records at launch)

The two JSONL exports use `geo-company-profile/v1` and preserve profile type, evidence, and provenance. The startup export includes only source-attributed YC records; it does not call general OSM businesses startups, and employee size is unknown unless evidenced. The GEO platform in sibling repository `geo-engine` has separate property storage and audit/experiment APIs. The export is deliberately a researcher-controlled handoff, not a direct write or an automatic audit run. Details and limitations are in [the integration contract](phase7_geo_platform_integration.md).

## SMB/SME target reconciliation

At the initial snapshot, Phase 7 had 45,504 of 55,911 selected general-business candidates processed, 10,407 unprocessed, and 7,172 strict contactable companies across all industries. The old YC-only comparison below is historical and is not the current target population. The professor's 5,000-company target means SMB/SME businesses, with no YC, funding, founding-age, accelerator, or technology requirement. The offline reconciliation identifies 370 verified, size-supported SMB candidates, 353 GEO-eligible SMBs, and 141 strictly contactable SMBs; the remaining gap is 4,859. Unknown-size records remain candidates for review. Hotel-tagged companies are excluded from the SMB count and remain in the separate 3,000-hotel target. See [the current SMB/SME reconciliation](phase7_smb_target_reconciliation.md).

The existing Phase 7 business worker stopped on 2026-10-17 with `quality_degradation_pause`; it is not running. It is not restarted by this hotel task. Inspect it with `PYTHONPATH=src uv run python scripts/phase7_service.py status` and resolve the quality pause before resuming.

The saved pilot crawl evidence contains titles, descriptions, content hashes, and text lengths, but not full exact page headings/paragraphs. Therefore no company-specific quoted edits or personalized final GEO reports were generated. The report template is `templates/company_geo_analysis_report_template.docx`. No LLM provider/model was configured or invoked, so there are no generated question sets or baseline/post model results. Do not present draft strategies as measured improvements.

The initial snapshot predates this work and is preserved at [phase7_new_targets_initial_progress.md](phase7_new_targets_initial_progress.md). No real email, contact form, public signup form, website edit, human approval, commit, or push was performed.
