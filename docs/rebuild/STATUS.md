# Outreach rebuild status — 2026-10-28

The rebuild continues on the requested feature branches: Outreach `codex/outreach-platform-rebuild` and GEO Engine `codex/outreach-durable-research`. No main-branch merge, new paid service, or full GEO research run was started.

## IMPLEMENTED LOCALLY

The V2 Outreach service, additive PostgreSQL migration/importer, Invitation V1 generation, Word Mail Merge bundle, Google Sheets parser, consent protections, and resumable V1 personalization backfill are implemented on the feature branch. Backfill calls are bounded to 100 candidates and can resume. The 244 saved V1 personalizations are preserved. The 392 historical test/workflow records remain isolated from candidate operations. No full GEO research is run before an invitation and valid consent.

The shared Word Mail Merge subject is `GEO research participation invitation`; each saved V1 record and preview retains its original company-specific subject. The bundle binds each recipient field to the selected contact and does not send messages.

## VERIFIED LIVE — NEON

A custom-format backup of the pre-migration operational database was created and validated at `/Users/melc/.config/geo-research-outreach-agent/rebuild-backup-20261027/neon-operational-before-outreach-v2.dump` (69,480,248 bytes). The full reconciled SQLite source snapshot and archive manifest remain protected outside the repository.

The migration was installed into the existing Neon `outreach` database as the additive `outreach_v2` schema. Existing public/legacy tables were preserved. The importer used transactional, versioned schema setup and idempotent bounded inserts; candidate IDs were retained. Direct PostgreSQL queries after the live invitation test confirmed:

- 71,325 companies and 15,262 hotels (86,587 candidates total)
- 52,077 contacts and 355 source-catalog rows
- 244 saved V1 personalizations; zero V2 fixtures
- zero missing candidate/contact source references or checksums
- zero duplicated raw source rows or source memberships in V2; the complete source archive remains in the protected snapshot
- zero V2 suppression rows, consistent with the reconciled source and legacy suppression counts
- one invitation batch and one invitation generated in the live staging test

The 392 isolated historical test/workflow rows were not imported as candidates. Historical source evidence and the external archive remain preserved. The database measured 839,122,944 bytes after migration; the pre-migration backup is the rollback path. See `LEAN-MIGRATION.md` and the migration reconciliation manifests for importer and provenance details.

## DEPLOYED AND VERIFIED LIVE — RENDER

The existing Free Render Outreach staging service runs the feature branch at commit `94df250427e139b65fc7fe6333bc7401e10f4f92`. Its `OUTREACH_DATABASE_URL` now points to the migrated Neon endpoint, following the user's authorization. The service deployment succeeded and `/health` returned healthy for the Outreach V2 service.

The real administrator signed in successfully. The authenticated session survived a page refresh, and an authenticated API request returned live Neon candidate rows. The dashboard loaded all 86,587 candidates. Live tests verified exact-name search, hotel and eligibility/personalization filters, next-page pagination, selection, and one-action package generation.

One eligible, READY hotel candidate was selected. Render generated and downloaded `batch-be1fb348b64c4bc18b2dac4ec57cb2e8.zip`, containing the Word mail-merge document, matching Excel and CSV recipient tables, previews, manifest, and instructions. Direct Neon queries confirmed the batch and its one invitation row persisted, with the selected candidate and recipient binding matching the invitation. The batch subject is the agreed shared subject; send status remains `UNVERIFIED`. No message was sent. A separate two-fixture local Word package was inspected in Word/Outlook preview and not sent.

## BLOCKED — REAL-COMPANY IDENTITY AND CONSENT

YES+YES is necessary but self-reported form data is not proof of company identity. The supported automatic route verifies the respondent's Google account against the named representative and original invitation recipient. The live Google Form does not collect a verified account address, and the Forms API project currently returns 403 because the API is disabled. Owner binding remains restricted to isolated owner-controlled fixtures. Ordinary real companies therefore cannot yet pass automatic identity authorization.

The specific dependency is an authorized independent identity capability: enable verified Google respondent identity collection and Forms API access for this form, or authorize a mailbox challenge/verification provider. No routine manual review has been introduced, and no unverified response can authorize research.

## VERIFIED LIVE / BLOCKED — GOOGLE SHEETS AND SCHEDULING

The live Sheet was read successfully, its six existing response rows and actual headers matched the new parser, and the parser maps those columns correctly. The Outreach feature branch now implements a bearer-authenticated, size-limited worker endpoint and PostgreSQL session advisory lock, with a default-branch GitHub Actions schedule file that invokes one bounded worker stage per tick. The repository is public, so standard GitHub-hosted runner usage is free under GitHub's documented billing policy. This is **implemented and locally tested only**: the deployed Render app does not include the endpoint, the required GitHub secrets are not configured, and the workflow file is not on the `main` default branch. No scheduled execution or persisted live worker run is claimed. GitHub documents that scheduled triggers can be delayed or dropped, so the 15-minute offset schedule relies on later idempotent ticks to recover work; persisted `worker_runs` must be checked for actual execution. No paid scheduler was provisioned.

## SECURITY RECOVERY — OWNER HANDOFF REQUIRED

Safe metadata identifies two exposed connection credentials, both using role `outreach_owner` and database `outreach`: the old endpoint `ep-lively-paper-b48qbrzb` in Neon project `geo-outreach-lean-staging` (`fragrant-grass-48724484`) / production branch `br-jolly-boat-b43lenfm`, and the migrated endpoint `ep-patient-morning-b4slbk83` in `geo-outreach-staging` (`jolly-feather-09079128`) / production branch `br-hidden-voice-b4yy2t28`. No credential value is recorded here. The latter is the intended V2 destination. The old endpoint remains referenced by Render `OUTREACH_DATABASE_MIGRATION_URL`; Render `OUTREACH_DATABASE_URL` was previously changed to the migrated endpoint. The pre-migration backup remains intact.

Neon Console also displayed a warning that `geo-outreach-staging` had passed 80% of its monthly free-plan allowance. No paid upgrade was made; monitor its remaining allowance to avoid an unexpected staging interruption.

Credential rotation must be completed by the owner in Neon Console because this browser workflow requires the user to enter and submit new credentials. For each named project, open the listed production branch, go to **Postgres → Roles**, choose `outreach_owner`, and use the role action to reset its password. Store the newly generated password in the password manager. In Render's existing Outreach staging service, update both `OUTREACH_DATABASE_URL` and `OUTREACH_DATABASE_MIGRATION_URL` with the connection string for the intended migrated project, production branch, role, and `outreach` database; keep the runtime URL pooled and migration URL direct. Then redeploy and verify `/health`, the admin session, live candidate counts, and a harmless migration connectivity check. Do not reuse either old password. Remove or replace the old credentials in local secret files after service recovery, and confirm neither old password authenticates. No destructive database reset or candidate reimport is needed.

The repository's tracked refs were scanned for committed PostgreSQL URI credentials; none were found. Credential values did appear in prior browser/tool output. The temporary credential handoff helper was stopped and the local clipboard cleared.

## VERIFIED LOCALLY — CLEAN V2 TEST PATH

The local V2 test suite now verifies the exact two allowlisted owner fixtures, their recipient bindings and websites, two distinct references and form links, frozen body/preview fields, shared subject, and no duplicate invitation associations. Consent tests cover positive and negative pairs, unknown/historical references, duplicate and conflicting submissions, and suppression. A worker-driven YES+YES fixture response creates its durable research job automatically; the same tick leaves that job blocked because real research execution is explicitly disabled pending provider authorization and budget. No synthetic report or provider call is counted. The V2 tests pass locally. The full legacy suite has unrelated failures in this feature checkout because ignored source snapshot databases are absent, and one old-schema test still expects a legacy approval state; those are not V2 live evidence.

The previously exported live one-candidate batch was created solely during the controlled dashboard deployment test and was not sent. It is still present because Neon writes are paused until the credential rotation is complete. No live V2 fixture rows or new combined live package were created during this continuation.

## DEPLOYED / BLOCKED — GEO ENGINE AND REPORT DELIVERY

The existing GEO Engine staging service runs `codex/outreach-durable-research` at `496c0fafb0f75abb7bf86db7631d96999dfa76ff`; its health route is live. The durable research API code is deployed and requires an authenticated service token. Staging is not ready for a genuine research run: research execution is not enabled and no authorized search provider credentials are configured. The Outreach continuation now also fails closed unless `GEO_OUTREACH_RESEARCH_ENABLED=true`; this gate is local-only until the feature branch is safely redeployed after credential rotation. The presence of an OpenAI key name in Render is not proof of usable or authorized real-provider execution. No fixture, health response, or HTTP 200 is counted as completed research.

Report generation and delivery orchestration are implemented locally, but no real consented research result or genuine report has been produced. Unattended report email is blocked because no authorized sender/mailbox provider and permissions are available. Actual sending remains disabled and unverified.

## Remaining end-to-end blockers

1. Enable an independent, automatic real-company identity verification capability for the response form.
2. Configure an authorized search provider and explicitly enable the GEO Engine's real research execution.
3. Provide a durable authorized Google Sheets scheduler without provisioning an unapproved paid service.
4. Authorize an unattended report-email provider and mailbox permissions.
5. Continue the bounded Invitation V1 backfill for eligible candidates as operational capacity allows; do not run full GEO research before invitation and valid consent.

The invitation handoff and one-action package generation work against live Neon data. Automatic consent, real GEO research, genuine report generation, and report delivery are not yet end-to-end verified.
