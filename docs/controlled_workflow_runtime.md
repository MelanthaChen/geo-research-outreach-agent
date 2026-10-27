# Controlled seven-step workflow runtime

The existing Research Queue generates invitations only from persisted, observed personalization. Its Send Emails action authorizes the selected batch internally. Generation and export never imply sending or consent. The controlled dispatch allowlist binds each of the two owner profile IDs to its exact website and original recipient; no real-company outreach is enabled by this change.

The active authorized sender is chen.yuxuan9@northeastern.edu through Word Mail Merge for Mac and Outlook Desktop. This is a local desktop workflow. Render cannot use it as an unattended API. The default `OUTREACH_MAIL_PROVIDER=outlook_desktop` returns an explicit blocked result instead of a simulated success. No Gmail OAuth or Microsoft Graph connection is assumed or created.

## Consent and follow-up

The existing six-question Google Form and spreadsheet remain authoritative. Interest and authorization must independently be YES. New invitations persist an explicit owner-fixture automation binding and website scope; a public reference or self-reported email alone never creates that binding. Unknown references, invalid timestamps, conflicting submissions and mismatched representatives cannot launch research. Historical invitations are preserved without retroactive automation authorization.

`POST /api/integration/tick` uses the existing `GEO_ENGINE_HANDOFF_TOKEN` service authentication. It reads the existing spreadsheet with `GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64`, imports only the two controlled fixture references, and resumes persisted accepted work. The same implementation is callable through `scripts/sync_google_form_responses.py`. A deployed scheduler must invoke it periodically; implementing or manually calling this endpoint does not prove automatic synchronization. Do not use a browser timer or a Render Free in-process loop as a substitute. No paid scheduler was provisioned.

The deployed GEO Engine must return an actual completed result. Its existing controlled-fixture baseline is explicitly INCOMPLETE, even when the upstream endpoint labels the run completed. Incomplete research cannot be delivered as a completed report. Valid completed documents and result JSON are stored durably in Neon; retrying delivery reuses the document and dispatch ledger rather than rerunning research.

## Deployment and validation

Deploy the existing feature branch to the existing staging service. `/health` exposes its commit; authenticated `/api/research/runtime` exposes the database hostname/name and nonsecret integration flags. Neither endpoint returns credentials. Existing data migrations are additive and repeatable.

`tests/test_fixed_workflow.py` uses isolated SQLite records and injected providers. It verifies exact recipient mapping, independent consent, stored content exports, restart recovery and repeated-operation suppression. These tests are not live email, Sheets scheduler or real model evidence. The dated execution report records those separately.
