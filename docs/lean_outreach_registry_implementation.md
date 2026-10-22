# Lean Outreach Registry Implementation

## Scope

The discovery SQLite databases remain immutable inputs. The lean registry promotes
only records that have:

- verified identity and official website;
- `VERIFIED_US` geographic evidence;
- `ELIGIBLE` research qualification; and
- an actionable public channel: an official contact form with a URL or an
  evidence-backed public business email channel.

Promotion creates a `PROSPECT` record. It does not verify an email, approve a
contact channel, approve an invitation, or authorize outreach.

## Audited staging funnel

| Channel | Companies | Hotels |
|---|---:|---:|
| Form only | 3,368 | 180 |
| Email only | 3,679 | 215 |
| Email and form | 1,398 | 70 |
| Actionable prospects | 8,445 | 465 |

The preceding counts exclude contact-page-only records. The 5,077 companies and
285 hotels with email evidence are not verified email populations; their contact
records remain pending review.

## Implementation

- `src/outreach_agent/lean_registry.py` contains the idempotent promotion and
  lean schema.
- `scripts/build_lean_prospect_registry.py` builds an isolated SQLite target or
  a PostgreSQL target supplied through `--target`.
- `tests/test_lean_registry.py` covers channel classification, exclusion of
  unknown/foreign/unverified records, pending contact review, idempotency, and
  source preservation.

The lean schema contains prospects, contacts, provenance, suppression/prior-
contact identifiers, and lifecycle events. It deliberately does not copy the
full discovery `source_row_json` payload into the production registry.

## Isolated verification

Command:

```bash
PYTHONPATH=src uv run python scripts/build_lean_prospect_registry.py \
  --business-db data/phase7_candidates.db \
  --hotel-db data/hotel_candidates.db \
  --target /tmp/lean-outreach-registry.db \
  --existing-registry data/research_registry.db \
  --report /tmp/lean-outreach-registry.json
```

Observed isolated output:

- 8,910 prospects;
- 14,592 contact records;
- 0 outreach-ready records;
- 0 owner-controlled records;
- original discovery files unchanged;
- rerun produced identical counts.

The generated SQLite target was 32.9 MB. An isolated local PostgreSQL 16 target
measured **69,688,343 bytes** (about 66.5 MiB) after the first migration.
Rerunning the migration produced the same byte count and row counts, so the
idempotent path did not add PostgreSQL bloat. The lean target is comfortably
below the current Neon Free 1 GB project limit.
The current all-candidate staging database remains separate and is not reduced
or rewritten by this implementation.

## Operational gates

```text
DISCOVERY -> PROSPECT (contact review pending)
          -> OUTREACH_READY (contact verified and not blocked)
          -> INVITED (approved and reconciled send)
          -> RESEARCH_PARTICIPANT (verified authorization and consent)
          -> GEO handoff
```

GeoAIResume remains an owner-controlled test fixture and is excluded from formal
company and hotel prospect counts. Consent review remains mandatory before GEO
handoff.
