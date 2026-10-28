# Lean Outreach V2 migration and archive recovery

## Storage decision

The existing Neon database measured 741,523,456 bytes before migration. Its largest existing table and index footprints were `candidate_companies` 284,147,712 bytes, `research_records` 131,768,320 bytes, `candidate_company_provenance` 117,489,664 bytes, and `full_screening_queue` 60,891,136 bytes. The existing public schema remains the legacy operational system and is not replaced.

A rollback-only PostgreSQL sample used 2,000 representative V2 candidates and 2,000 contacts. It measured 892,928 bytes for candidates, including the primary key and domain index, and 1,687,552 bytes for contacts, including the primary key, candidate ID index, and email index. Extrapolated to 86,587 candidates and 52,077 contacts, that is 82,599,301 bytes before the additive archive-reference fields and the composite candidate filter index. The sample omitted the contact `raw` JSON because that payload duplicates the backed-up source archive.

The 244 saved V1 personalizations average 2,493.5 bytes (2,412-byte median, 2,772-byte p95, 3,533-byte maximum). The remaining 19,641 currently eligible candidates can be processed by the existing resumable V1 backfill in calls of at most 100 websites. Even budgeting 3,533 bytes for every missing personalization adds about 69.4 MB before PostgreSQL compression. This was a pre-import estimate only. The actual Neon size and persisted row counts were measured after import and are recorded below.

## What the cloud import stores

The additive `outreach_v2` schema contains the 71,325 company and 15,262 hotel identities, all 52,077 contact channels, searchable and screening fields, saved Invitation V1 personalization, suppression state, the 355-row source catalog, and the new workflow state. Candidate and contact `source_ref` and `source_checksum` values point to either an immutable source row or the corresponding canonical snapshot row. The importer keeps candidate IDs unchanged and verifies every contact maps to a candidate.

The cloud importer does not copy 1,044,070 serialized source-row payloads, 2,173,051 source memberships, or the duplicate JSON in contact rows into the operational schema. The operational `source_rows` and `source_memberships` tables stay empty. Existing candidate evidence, source URLs, qualification fields, contact evidence, and provenance hashes remain in the V2 rows, while the original complete archive remains available separately.

The full reconciled SQLite snapshot is stored outside the repository at:

`/Users/melc/.config/geo-research-outreach-agent/rebuild-backup-20261027/outreach-v2-migration-consistent.db`

Its protected manifest is `outreach-v2-source-archive-manifest.json`. The snapshot is 1,833,836,544 bytes, mode `0600`, SHA-256 `62c665589b607e32d86bfdbc6bafe9fadc30f48fd04b75104797ec96077e6df0`, and passed SQLite `PRAGMA integrity_check`. It contains all 86,587 candidates, 52,077 contacts, 1,044,070 source rows, and 2,173,051 source memberships. The 392 isolated historical test/workflow rows remain in this archive and do not enter the V2 candidate or contact tables.

## Migration safety and recovery

`Database.initialize()` creates only the `outreach_v2` schema and applies additive, versioned migrations inside a PostgreSQL transaction. The importer verifies the protected archive checksum and source reconciliation, then inserts candidates and contacts in bounded 500-row transactions using conflict-safe inserts. It never updates or drops legacy tables. If a run stops, rerun the same import against the same snapshot; committed batches are idempotent. The importer refuses to mix lean imports with copied source-row payloads and reconciles persisted candidate/contact counts before returning success.

PostgreSQL text cannot store an embedded NUL byte. The reconciled snapshot contains one such byte in a candidate description, so the cloud display copy replaces that byte with U+FFFD while keeping the source-row checksum tied to the untouched archived record. The archive remains byte-for-byte unchanged.

To verify or recover the archive, use the protected snapshot path and manifest above. Recompute the file SHA-256 and compare it to the manifest, then run `sqlite3 <snapshot-path> 'PRAGMA integrity_check;'` and verify the listed table counts. For operational recovery, rerun `python -m outreach_v2.migrate --url <existing-neon-url> --lean-operational-snapshot <snapshot-path> --archive-manifest <manifest-path> --report <report-path>` from this feature branch. Supply the existing Neon URL through the protected operator environment; never put it in the command history or report. The separately saved PostgreSQL custom-format backup at `rebuild-backup-20261027/neon-operational-before-outreach-v2.dump` can restore the pre-migration legacy database if an operator ever needs to recover it.

This is a protected local archive on the owner's machine, not an offsite cloud archive. No new object-storage service has been added. The complete source data is recoverable from this backed-up location while a shared remote archive destination remains unapproved.


## Verified live import (2026-10-28)

The additive import is installed in the existing Neon `outreach` database under `outreach_v2`; no legacy public tables were dropped or overwritten. Direct PostgreSQL verification after the live package-generation test confirmed 71,325 companies, 15,262 hotels, 52,077 contacts, 355 sources, 244 saved V1 personalizations, and zero imported fixture candidates. Candidate/contact source references and checksums are all present. The operational source-row and source-membership tables are empty by design; the complete source archive and the 392 isolated historical test/workflow rows remain in the protected local snapshot and manifest.

The database measured 741,523,456 bytes before migration and 839,122,944 bytes after migration. The pre-migration custom-format backup at `rebuild-backup-20261027/neon-operational-before-outreach-v2.dump` is 69,480,248 bytes and passed `pg_restore --list`.

Render staging now connects to this migrated database. One live Invitation V1 batch and invitation row were created through the dashboard. The batch stored the agreed shared subject and remains `UNVERIFIED`; no message was sent.
