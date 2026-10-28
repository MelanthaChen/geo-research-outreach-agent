# Outreach rebuild checkpoint

The replacement is implemented in `src/outreach_v2` on `codex/outreach-platform-rebuild`; the companion durable research-job API is in the GEO Engine feature branch `codex/outreach-durable-research`. The original Render staging service and Neon operational schema remain unchanged.

The source inventory covers 25 SQLite databases and 113 CSV files. All 139 inventoried files are represented in 355 source relations, and every relation reconciles its expected and processed row counts. The local reconciliation snapshot contains 71,325 company candidates, 15,262 hotel properties, 52,077 contact channels, and 2,173,051 source memberships. It retains 1,044,070 original source rows; 392 historical test/workflow rows are marked `ISOLATED`. Candidate and hotel identities remain separate. The dashboard service queries all 86,587 candidates from the snapshot. Exact counts and the row-level isolation manifest are in `migration-reconciliation.json` and `migration-exclusions.json`.

Seven existing Neon mirror tables were compared with their local source rows and fingerprints; every count and fingerprint matched. This was a read-only verification. The new operational schema has not been migrated to Neon, and Render has not been switched. The migration snapshot is a local SQLite validation artifact, not the production database.

The personalized Invitation Email V1 is frozen. The user selected the single shared Word bulk subject `GEO research participation invitation`; original company-specific V1 subjects remain in saved records and previews. Invitation export uses one Generate & Download action. Historical drafts, simulated sends, fixture invitations, submissions, research, and reports stay outside the operational workflow and are recorded as isolated source evidence.

Isolated Outreach tests pass (16 tests) and the GEO Engine durable-job tests pass (5 tests). Dashboard queries against the full local snapshot return all 86,587 candidates. The seven-table Neon mirror reconciliation is exact.

End-to-end status remains partial: no invitation has been sent, no fresh consent has been submitted, and no new research/report delivery has been run. The user has no authorized unattended report sender yet, so automatic report delivery cannot be validated or enabled. The old staging service, old staging database, Google Sheet, and GEO Engine deployment have not been modified or cleaned; the manifest does not claim external deletion.
