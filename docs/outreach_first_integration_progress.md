# Outreach-first integration progress

Implemented a local simulated lifecycle for the corrected outreach-first
workflow. Existing Phase 6/7 databases, hotel checkpoints, campaign sidecar,
and `geo-company-profile/v1` exports remain separate and unchanged. The new
registry is a separate SQLite read model at `data/research_registry.db` when
initialized; tests use temporary databases.

## Implemented foundations

- Unified research records preserve stable profile ID/version, company versus
  property type, identity, evidence, contacts, qualification, recruitment,
  questionnaire, consent, GEO status, and monitoring status.
- Owner-controlled fixtures are excluded from cohorts, recruitment, and GEO
  handoff by hard lifecycle guards.
- Preliminary screening stores 1–3 evidence-backed opportunities, exact URLs
  and excerpts, provider/model/prompt provenance, and
  `PRELIMINARY_UNVALIDATED`. Unvalidated percentage uplift claims are rejected.
- Opaque HMAC-bound questionnaire tokens are stored only as hashes. Duplicate
  submissions are idempotent; consent wording/version and timestamps persist.
- GEO handoff requires explicit website-analysis consent and is idempotent by
  profile/version. It does not call or write to GEO Engine.
- Email drafts preserve versions, recipients, preview hashes, and approval.
  The only available send operation records a disabled-provider simulation;
  it cannot deliver email.
- Reply classification is advisory and stored separately from consent.
- Existing dashboard server now exposes paginated read-only registry views at
  `/api/research/records` and CSV export at `/api/research/records.csv`.

## Simulated lifecycle order

`register profile → preliminary screen → draft invitation → human approval →
local simulated send → opaque questionnaire → explicit consent → idempotent
GEO handoff`.

No deep GEO research is possible before consent. No form submission, real email,
paid experiment, deployment, or monitoring call is implemented here.

## Remaining integrations

- Production email provider, suppression/unsubscribe enforcement, and verified
  delivery/reply ingestion still require configuration and review.
- Questionnaire hosting/webhook verification is not connected; public Google
  Forms cannot update the registry automatically.
- GEO Engine authenticated property mapping and job transport remain external;
  the Phase 1 versioned local adapter/result importer remains the boundary.
- Dashboard UI controls for bulk review and expanded filters are not yet wired;
  the server-side paginated registry endpoint is in place.
- Monitoring ingestion and reviewed external report delivery remain pending.

## Verification

New lifecycle tests cover cohort exclusions, identity preservation, screening
provenance, consent gating, token security, duplicate submissions, reply
classification, approval enforcement, handoff idempotency, owner-fixture
blocking, and no GEO research before consent. All email/form/GEO operations in
the fixture lifecycle are local simulations.

