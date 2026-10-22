# Neon + Render cloud deployment preparation

This document prepares, but does not provision, the cloud deployment. Production
credentials, databases, and services must be created and migrated by an
operator after staging acceptance.

## Current data audit

Read-only counts from the checked-out databases:

| Store | Records | Role |
|---|---:|---|
| `data/phase7_candidates.db` / `candidates` | 57,664 | company candidate source; never written by the Outreach lifecycle |
| `data/hotel_candidates.db` / `candidates` | 15,262 | hotel candidate source; never written by the Outreach lifecycle |
| `data/research_registry.db` / `research_records` | 72,927 | lifecycle read model, including company/hotel/test records |

The Phase 7 and hotel stores remain SQLite source datasets. The durable cloud
registry is the Outreach `research_records` database; do not upload or mutate
the source stores as part of application startup.

The checked-out source stores remain immutable SQLite inputs. They are now
copied into a durable candidate read model by the source migration below; the
operator must still stage and validate that migration before the production
dashboard is expected to show the full candidate pool.

That source migration is now implemented by
`scripts/migrate_candidate_sources.py`. It creates durable `candidate_companies`,
`candidate_hotels`, provenance, contact, and company-screening mirror tables,
then upserts source facts into the existing `research_records` lifecycle model.
The Research Queue continues to read `research_records`; the mirror tables
preserve source-specific fields and complete source-row JSON for reconciliation.

Source identity is deliberately not domain-only: company/property
`candidate_id` remains the primary key, so multiple hotel properties sharing a
chain domain are retained as separate physical properties.

## Render services

- Outreach web service: `render.yaml` in this repository.
- Outreach Sheets Cron Job: same Blueprint, every five minutes, one-shot and idempotent.
- GEO Engine web service: `render.yaml` in the GEO Engine repository. Its `DATABASE_URL` must point to the existing GEO Engine PostgreSQL database when one already exists; the Blueprint no longer provisions a replacement database.
- Separate PostgreSQL databases are required. Outreach uses Neon; `OUTREACH_DATABASE_URL` is the pooled Neon runtime URL and `OUTREACH_DATABASE_MIGRATION_URL` is the direct Neon URL for migrations. `DATABASE_URL` belongs only to GEO Engine and must remain its existing production/staging connection.
- GEO Engine integration JSONL is mounted at `/var/data/integration` on a persistent disk. The adapter still performs only property registration/mapping.

The Outreach web service requires `OUTREACH_ALLOW_REMOTE=true` plus either
`OUTREACH_DASHBOARD_TOKEN` or basic-auth credentials. Render health checks use
the unauthenticated `/health` route; API routes require authentication when
bound remotely.

## Environment variables

Outreach Render + Neon:

```text
OUTREACH_DATABASE_URL=postgresql://...          # Neon pooled runtime URL, sslmode=require
OUTREACH_DATABASE_MIGRATION_URL=postgresql://... # Neon direct migration URL, sslmode=require
GEO_ENGINE_API_URL=https://geo-engine-....onrender.com
GEO_ENGINE_HANDOFF_TOKEN=<shared random secret>
OUTREACH_ALLOW_REMOTE=true
OUTREACH_DASHBOARD_TOKEN=<random secret>
RESEARCH_QUESTIONNAIRE_SECRET=<random secret>
RESEARCH_QUESTIONNAIRE_BASE_URL=https://<authorized-questionnaire-host>
GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64=<base64 service-account JSON secret>
GEO_RESEARCH_SHEETS_RANGE=Form Responses 1!A:Z
```

GEO Engine:

```text
APP_ENV=production
DATABASE_URL=postgresql://...                  # existing GEO Engine database only
OPENAI_API_KEY=<secret; required by current settings>
GEO_ENGINE_HANDOFF_TOKEN=<same shared random secret>
GEO_ENGINE_INTEGRATION_STORE=/var/data/integration
PUBLISH_DRY_RUN=true
```

No credential or token belongs in a repository, Blueprint literal, log line,
or exported database snapshot.

## Migration procedure

1. Create separate Neon staging branches/databases for Outreach. Do not point them at production.
2. Install Outreach cloud dependencies: `pip install '.[gmail,cloud]'`.
3. Run a dry-run count from the source registry:

   ```sh
   PYTHONPATH=src uv run python scripts/migrate_research_registry.py \
     --source data/research_registry.db \
   --target-url "$OUTREACH_DATABASE_MIGRATION_URL" --dry-run
   ```

4. Take a verified SQLite backup and checksum it before migration.
5. Run the migration once against the Neon staging direct URL. The source is read-only; the target uses conflict-safe inserts.
6. Compare per-table counts and foreign-key/invitation/submission/handoff relationships.
7. Run dashboard read-only checks, Sheets dry-run, owner-controlled consent review, and GeoAIResume handoff.
8. Repeat migration verification against a fresh staging target before production approval.
9. For production, place the service in maintenance/read-only mode, take a final backup, migrate, compare counts, and only then switch the web service to PostgreSQL.

The migration command does not delete, rewrite, or reset the source database.
It must not be run against a production target until a backup and rollback
window are approved.

Candidate-source staging commands:

```sh
PYTHONPATH=src uv run python scripts/migrate_candidate_sources.py \
  --business-db data/phase7_candidates.db \
  --hotel-db data/hotel_candidates.db \
  --registry "$OUTREACH_DATABASE_MIGRATION_URL" \
  --report /tmp/candidate-source-reconciliation.json \
  --dry-run

PYTHONPATH=src uv run python scripts/migrate_candidate_sources.py \
  --business-db data/phase7_candidates.db \
  --hotel-db data/hotel_candidates.db \
  --registry "$OUTREACH_DATABASE_MIGRATION_URL" \
  --report /tmp/candidate-source-reconciliation.json
```

The dry-run writes only an ephemeral local target and never opens the supplied
registry for writes. The real command is rerunnable and uses conflict-safe
upserts. Review the report's source counts, duplicate-domain list, missing
relationship counts, and field checksums before accepting staging.

## Startup and recovery

GEO Engine:

```sh
cd geo-engine/backend
pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port "$PORT"
```

Outreach web:

```sh
pip install '.[gmail,cloud]'
python scripts/phase5b_dashboard.py --host 0.0.0.0 --port "$PORT"
```

Sheets Cron:

```sh
python scripts/sync_google_form_responses.py --registry "$OUTREACH_DATABASE_URL"
```

Every handoff is retry-safe and property registration is idempotent. A failed
handoff remains recorded as `HANDOFF_FAILED`; a later retry can recover it.
Withdrawal and quarantined conflicting responses remain blocking states.
No audit, citation test, experiment, deployment, monitoring, or paid job is
started by the handoff endpoint.

## Backup, rollback, and alerts

- Use Neon branch/backup/restore controls for Outreach and the existing GEO Engine database's backup/PITR procedure; retain an independent logical dump before migration.
- Alert on Render web health failures, Cron failures, repeated `HANDOFF_FAILED`, and stale Sheets `updated_at`/status records.
- Roll back application code by redeploying the prior immutable version. Roll back data by restoring a new database from the verified backup and switching the service connection string; never run destructive reset scripts in production.
- The integration endpoint authenticates a bearer token and exposes only `profile-import` plus a non-sensitive health result.

## Cost estimate

Render compute still includes two web services and one low-resource Cron Job;
Outreach database cost moves to the selected Neon plan, while GEO Engine's
existing database cost remains unchanged. Exact monthly cost depends on the
Neon plan, compute sizes, bandwidth, and the existing GEO Engine deployment;
recheck both providers before provisioning: [Render pricing](https://render.com/pricing)
and [Neon pricing](https://neon.tech/pricing).

## Staging acceptance gate

Production deployment remains blocked until staging proves:

- migrated counts and referential integrity match;
- candidate source mirror counts/checksums match and the full Research Queue is visible through the cloud registry;
- Sheets Cron reads without writing real responses;
- consent review remains explicitly human-controlled;
- GeoAIResume creates exactly one GEO Engine Property after a verified review;
- restart/retry preserves state and does not duplicate the Property;
- no research execution job starts automatically.
