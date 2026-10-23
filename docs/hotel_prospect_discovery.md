# Automated hotel prospect discovery

The hotel target is implemented as an extension of the existing Phase 7
property discovery and lean promotion pipeline. It does not create a hotel
CRM or a second lifecycle store.

## Source strategy

The existing OSM/Geofabrik hotel worker remains the bounded verifier and
contact-evidence collector. The scheduled control plane now has a reproducible
public-source mode backed by one bounded OpenStreetMap Overpass request per
run. It rotates through a fixed list of US metropolitan bounding boxes,
requests hotel-tagged properties only, preserves the OSM element ID, and
attributes ODbL data. It fetches at most 100 elements per region and uses the
existing first-party website/contact verifier before promotion.

The legacy JSON/CSV manifest adapter remains available for staging, recovery,
and deterministic tests. HTTPS sources are required; source responses are
capped at 5 MB, and neither adapter bypasses robots rules, CAPTCHAs,
authentication, or access controls.

Each source row must preserve a property-level source ID, name, physical
address/location, official website, source URL/license, verification and GEO
qualification states, and compact contact evidence. The raw manifest remains
outside the operational Neon registry.

## Promotion gates

`scripts/hotel_prospect_job.py run` promotes a row only when it has:

- a verified physical US hotel location;
- verified identity and an official website;
- `ELIGIBLE`/`QUALIFIED` research status; and
- an actionable public business email or official contact-form URL.

Promotion creates `research_records.profile_type='hotel_property'` with
`promotion_state='PROSPECT'`, `contact_verification_status='CONTACT_REVIEW_PENDING'`,
and `prospect_contacts.email_verification_status='NOT_VERIFIED'`. Contact
review, outreach approval, consent review, and GEO handoff remain separate.
Chain domains are not identities: the property name and physical address are
included in the stable identity hash.

## Checkpoints and controls

hotel_discovery_jobs, hotel_discovery_events, and
hotel_discovery_source_stats are created in the same lean database as the
lifecycle registry. The job stores only counters, source cursor, outcome
reason, and compact event metadata; source stats track request count, raw
yield, verification, duplicates, rejection, and errors. Re-running a source is
idempotent by job/source ID, source identity, and existing suppression/source
identifiers. `HOTEL_DISCOVERY_TARGET` is represented by
`HOTEL_PROSPECT_TARGET`, defaulting to 1,000. The job pauses automatically at
the target and supports authenticated Dashboard pause/resume controls.

The Render cron definition is present but `HOTEL_DISCOVERY_ENABLED=false`.
Cloud discovery must not be enabled until staging, source attribution/terms,
rate limits, and yield review are approved. When explicitly enabled in staging,
set `HOTEL_DISCOVERY_SOURCE_MODE=OVERPASS`; no manually prepared manifest is
required. Set `HOTEL_DISCOVERY_SOURCE_MODE=MANIFEST` with
`HOTEL_DISCOVERY_SOURCE=/path/or/https/url` for recovery and tests.

## Expected yield and runtime

The current 465 promoted hotel prospects are the baseline. The job needs 535
additional promotions. Yield cannot be claimed from raw crawler volume: the
existing pilot's observed verification and contact rates are the only local
evidence, and a new source must be measured in bounded batches. At a batch
size of 25, 22 successful promotion batches are required if every row passes;
actual runtime is source- and access-dependent. The cron should remain hourly
or slower until measured source response rates and access-block rates support
that schedule.

## Local commands

Autonomous staging/operator runs use source mode OVERPASS and omit --source:

    PYTHONPATH=src HOTEL_DISCOVERY_ENABLED=true HOTEL_DISCOVERY_SOURCE_MODE=OVERPASS \
      uv run python scripts/hotel_prospect_job.py run \
      --registry /tmp/lean-hotel-registry.db --target 1000 --batch-size 25

The manifest command below remains available for deterministic recovery and
tests; a source manifest is not required for normal operation.

```sh
PYTHONPATH=src HOTEL_DISCOVERY_ENABLED=true \
  uv run python scripts/hotel_prospect_job.py run \
  --registry /tmp/lean-hotel-registry.db \
  --source /path/to/approved-hotel-manifest.json \
  --target 1000 --batch-size 25

PYTHONPATH=src uv run python scripts/hotel_prospect_job.py status \
  --registry /tmp/lean-hotel-registry.db
```

The source manifest is a staging/operator input and must not be committed.
The job does not send email, approve contacts or consent, or launch GEO
audits, experiments, monitoring, deployments, or paid evaluations.
