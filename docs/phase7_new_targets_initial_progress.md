# Initial Research-Target Snapshot (Historical)

Historical snapshot recorded before the SMB/SME target correction. The former “5,000 startups” wording incorrectly narrowed the target to venture/startup source attribution. The professor's target is 5,000 small and medium-sized businesses. The current denominator and policy are in [the Phase 7 SMB/SME reconciliation](phase7_smb_target_reconciliation.md); the YC-only figures below are not the target population or a current gap calculation. The separate 3,000-hotel target remains unchanged.

## Phase 7 startup-worker state

The Phase 7 LaunchAgent is loaded in the user launchd domain but its job has exited; no crawler process is currently running. The persisted worker state says it stopped cleanly at `2026-10-17T00:15:46Z` with `quality_degradation_pause`, after the quality guard detected that the current batch yield had fallen below its configured floor. It did not fail from an unhandled exception. The last saved Phase 7 checkpoint is present, and the current database passes its integrity check.

At the time this initial snapshot was taken, the status CLI raised `UnboundLocalError` when the worker was not running because it initialized its service-state dictionary only in the live-PID branch. The counts below were therefore read directly from the saved SQLite database and worker-state JSON, not from the broken CLI status output. The status command was subsequently fixed and covered by a regression test.

| Metric | Saved value |
|---|---:|
| Candidate records / distinct domains | 57,664 / 57,664 |
| Deterministically selected for Phase 7 | 55,911 |
| Selected candidates processed | 45,504 |
| Selected candidates not yet processed | 10,407 |
| Phase 7 verified identities (all industries) | 20,879 |
| Strict contactable companies (all industries) | 7,172 |
| Public-business-email companies (all industries) | 5,040 |
| Confirmed-form companies (all industries) | 2,988 |
| Cumulative HTTP attempts | 149,951 |
| Persisted Phase 7 wall runtime | 83,523.7 seconds |
| Saved worker stop reason | `quality_degradation_pause` |

The all-industry contactable count is not itself a confirmed SMB count. Use the size-evidence reconciliation before reporting progress toward the SMB/SME target.

## Startup classification evidence

The only explicit startup-source records in the current candidate database are 477 companies attributed to the Y Combinator Consumer/Public Startup directories. Source attribution is strong evidence of startup affiliation, but the historical source inventory flags automated extraction/reuse as requiring authorization; do not treat it as approval for additional YC acquisition. OSM commercial categories do not establish that a business is a startup.

| YC-attributed classification | Count |
|---|---:|
| Unique YC-attributed records | 477 |
| Website identity verified | 427 |
| Identity verified and GEO eligible | 376 |
| Verified + GEO eligible + public business email only | 124 |
| Verified + GEO eligible + confirmed contact form only | 14 |
| Verified + GEO eligible + both email and form | 22 |
| Verified + GEO eligible + neither usable channel | 216 |
| Verified YC-attributed records not GEO eligible (needs review) | 51 |

The 427 identity-verified, YC-attributed companies are the maximum defensible *source-classified startup* baseline in this database, not 427 fully qualified, human-reviewed startups. Classification confidence is high for YC affiliation based on source provenance, while current operating status, independent-company identity, and fit still require their separate evidence and review. Among those 427, 146 have a public business email (including companies with both channel types), 36 have a confirmed form (including companies with both), 22 have both, and 267 have no usable channel in the saved evidence. Restricting to the 376 also GEO-eligible records, 160 have at least one currently qualifying saved public business email or confirmed form, with the 22 having both counted once.

The old startup-specific gap calculation is superseded. The general OSM pool includes potentially relevant SMBs but contains no company-size evidence, so those records remain `SIZE_UNKNOWN` until supported by a source or human review. OSM business tags do not establish size or independent ownership.

## Hotel classification evidence

The current OpenStreetMap-derived pool has 977 records with the explicit `tourism=hotel` category. This is a candidate classification signal, not independent proof that every record is an operating hotel property. Existing Phase 7 results show:

| Hotel candidate state | Count |
|---|---:|
| `tourism=hotel` candidates | 977 |
| Identity verified + GEO eligible | 378 |
| Identity verified, GEO needs review | 15 |
| Discovered, not yet processed | 95 |
| Ambiguous identity | 157 |
| Fetch failed | 141 |
| Access blocked | 86 |
| Robots denied | 66 |
| Insufficient evidence | 26 |
| Rejected mismatch | 13 |

The current regional extract is therefore insufficient for the 3,000 verified-hotel target even if every remaining record were to qualify. Broader, licensed OSM extracts or another openly licensed structured lodging source will be needed; each property still requires first-party identity and operating evidence. The original OSM source is Geofabrik's US Northeast extract and the rows preserve OSM element URLs and ODbL attribution.

## Interpretation and safety

These figures are an offline snapshot of saved project records, not live re-verification. Contact-channel availability is separate from human approval and outreach authorization. No contact channel has been sent a message and no website form has been submitted. The original 77-company database, 537-company cohort, campaign sidecar, prior contact-review decisions, and saved Phase 7 evidence remain the protected baseline.
