# Candidate-source reconciliation

Generated from the checked-out SQLite sources using
`scripts/migrate_candidate_sources.py --dry-run` on 2026-10-22. The source
databases were opened read-only.

| Source | Canonical rows | Provenance rows | Contact rows | Screening rows |
|---|---:|---:|---:|---:|
| Company `phase7_candidates.db` | 57,664 | 110,407 | 24,859 | 57,664 |
| Hotel `hotel_candidates.db` | 15,262 | 9,787 | 850 | not applicable |

The existing lifecycle registry remains separate and authoritative for consent,
invitations, replies, handoffs, and queue state:

| Registry relation | Rows |
|---|---:|
| `research_records` | 72,927 |
| `full_screening_queue` | 145,854 |

The queue count is larger because a profile can occur once per screening job;
it is not a unique-company count. The registry also contains owner-controlled
test and historical lifecycle records in addition to the source candidate
cohorts.

## Reconciliation checks

- Company candidate IDs missing from provenance: **0**.
- Company candidate IDs missing from contacts: **0**.
- Hotel candidate IDs missing from provenance: **0**.
- Hotel candidate IDs missing from contacts: **0**.
- Duplicate company canonical domains: **0**.
- Duplicate hotel canonical-domain keys: **146 domain keys**. These are retained
  because hotel identity is the physical-property `candidate_id`; chain domains
  are not unique property identifiers.
- Company canonical-domain field checksum:
  `aa310608d5fea03339eaddc2a4f5cb4235fe7f6c4b8edc12c44187da2f9cab0d`
- Hotel canonical-domain field checksum:
  `e97cc3c439c7fde793fc98dc576e9dc9cbdd2fa40def591a6a999977f525e143`

The migration also records complete source-row JSON and per-row SHA-256 values
in the durable mirror tables. Rerunning the migration uses primary-key
conflict-safe upserts and does not duplicate candidates, contacts, provenance,
or screening rows.

## Cloud read path

The Research Queue uses `OUTREACH_DATABASE_URL` and reads migrated
`research_records` through the existing lifecycle API. In cloud mode the legacy
Phase 5 SQLite company view is not loaded, preventing an accidental fallback
to bundled local files. Source-specific data is available from the durable
`candidate_companies` and `candidate_hotels` mirror tables.
