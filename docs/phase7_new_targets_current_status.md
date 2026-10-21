# Phase 7 new-target status

Snapshot: 2026-10-21. The pre-change audit is preserved in [phase7_new_targets_initial_progress.md](phase7_new_targets_initial_progress.md); the regional hotel pilot and worker history are in [phase7_hotel_pilot_report.md](phase7_hotel_pilot_report.md).

## SMB/SME target

The Phase 7 business worker last stopped on 2026-10-17 with `quality_degradation_pause`, at 45,504 of 55,911 selected candidates processed and 10,407 pending. Its database integrity check is `ok`. This reconciliation was offline and did not restart discovery. Across the complete 57,664-record Phase 7 pool, 7,172 companies already meet the strict contactability rules.

Under the saved-size-evidence policy, 370 records have verified identity, a substantive first-party website signal, supported SMB size, and no unresolved same-name/location identity collision. Of these, 353 are GEO eligible and 141 also have a qualifying email or confirmed first-party form. The remaining gap to 5,000 contactable SMBs is 4,859. These are the only records currently countable toward that target from this saved pool; size-unknown companies remain review candidates, not ineligible businesses. No venture-funding, YC, recent-founding, accelerator, or technology-sector requirement is applied.

The all-industry Phase 7 strict-contactable distribution is 4,184 email-only, 2,132 form-only, 856 with both, and zero with neither. The 370 supported verified-SMB records include 131 email-only, 4 form-only, and 6 with both among the 141 contactable SMBs. The full pool has 50,492 records without a strict qualifying email/form channel in the current export; some may still have unverified or lower-quality channel evidence.

Among the strict-contactable records, 6,823 non-hotel businesses have unknown size and remain candidates for evidence collection or human review. One further non-hotel contactable record has saved SMB size evidence but is held for a same-name/location identity collision review. The reconciliation does not infer size from industry, website appearance, source, age, or location.

The Phase 7 candidate data includes 978 OSM `tourism=hotel`/`amenity=hotel` records, including 207 strict-contactable records. They are excluded from the 5,000 SMB target and remain separate from the 3,000-hotel target and its worker. See [the reconciliation report](phase7_smb_target_reconciliation.md) for source provenance, sector/geography classifications, size policy, review queues, and limitations.

## Hotels

The isolated hotel database has 15,262 OSM-tagged named property records from Geofabrik US Northeast, California, Texas, and Georgia extracts. There are 2,026 canonical domains, 8,494 tagged website values, and 12 known third-party URLs excluded from inspection. Property-level IDs keep locations separate even when websites are shared. This is candidate supply, not verified hotel count.

The 25-property bounded pilot produced 11 identity-verified properties, 10 GEO-eligible properties, and 9 with a saved qualifying first-party contact channel. The first three production batches processed 75 more properties with 212 HTTP attempts in 200.37 seconds; they produced 19 additional verified identities and 17 additional GEO-eligible outcomes. Access-blocked shares were 48%, 32%, and 52%; the 40% circuit breaker paused after batch three. After resuming with domain-diverse, independent/unknown and chain-indicated round-robin selection, two batches each produced 8 verified identities and 8 GEO-eligible properties; access blocks were 3/25 (74 attempts) and 5/25 (73 attempts). This is encouraging but not a sustained yield result. The next batch was in progress at the latest status snapshot: 152 actually inspected including the pilot, 46 identity verified, 42 GEO eligible, 24 with contact evidence (16 email, 11 forms, 3 both), and 8,330 pending. The status includes two partial-batch properties and may change. Contact channels remain pending human review.

The hotel LaunchAgent is independent from the Phase 7 business worker, uses a distinct database and lock, one active inspection at a time, and finite caps. Current PID and live counts should be read rather than inferred from this snapshot:

```bash
PYTHONPATH=src uv run python scripts/phase7_hotels.py status
PYTHONPATH=src uv run python scripts/phase7_hotels.py stop
```

At the latest status read the worker was running as PID `94476`, with 359 HTTP attempts persisted from completed batches and cumulative configured caps of 25,002 requests and 172,802 seconds; use the status output as source of truth because the current batch may change these counts. A batch that reaches 40% access blocks pauses at the batch boundary. Do not bypass blocked sites or robots rules.

## Contact-channel handling

Company contact coverage is now separated into `EMAIL_ONLY`, `FORM_ONLY`, `EMAIL_AND_FORM`, `NO_CONTACT_CHANNEL`, and `NEEDS_REVIEW`, with dashboard counters. Form-only records remain in a dedicated manual form-review queue with the official form URL and prepared message; email campaign selection remains email-only, and the queue explicitly records `NOT_SUBMITTED`. The route is `/api/campaigns/form-review-queue.csv`. Tests cover channel classification and the form queue. No message or form submission was performed.

## GEO research handoff

`phase7_hotels.py profiles` creates deterministic `geo-company-profile/v1` JSONL exports for identity-verified, GEO-eligible hotel properties and a legacy YC-attributed company subset. The latter is a handoff slice, not the SMB/SME target population. It is a one-way, researcher-controlled contract to the separate sibling `geo-engine` repository, whose audit and experiment storage/API are not modified by this task. See [the integration contract](phase7_geo_platform_integration.md).

The shared profile distinguishes hotel-specific fields (amenities, chain/independent signal, room evidence, operating status) from startup fields (YC source attribution, industry/location, size evidence status). The company report template is [company_geo_analysis_report_template.docx](../templates/company_geo_analysis_report_template.docx); its rendered two-page layout was visually inspected. Exact copy recommendations and company-specific Word reports are not generated from the current pilot because its saved crawl evidence lacks the source headings and paragraph text required for accurate quotations. No LLM provider/model was run, so there are no baseline or post-optimization question results.

## Integrity and validation

The full Python test suite passed after the current changes. JavaScript syntax validation passed. SQLite integrity checks passed for the hotel database, Phase 7 candidate database, frozen 77-company database, original 537-company cohort, and campaign sidecar. Full `git diff --check` still reports pre-existing trailing whitespace in the already-modified `data/phase7_contact_channel_audit.csv`; the whitespace is inside saved website evidence and was preserved. No original company database, review decision, campaign record, or frozen export was intentionally edited by this task. No commit or push was made.
