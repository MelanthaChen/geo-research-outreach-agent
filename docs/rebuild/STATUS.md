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

The live Sheet was read successfully, its six existing response rows and actual headers matched the new parser, and the parser maps those columns correctly. No durable automatic sync scheduler is deployed. The existing free web service is not an unattended scheduler; the available Render cron configuration would require a paid resource and was not provisioned. Automatic sync remains blocked until an already-authorized scheduler is available or a paid resource is explicitly approved.

## DEPLOYED / BLOCKED — GEO ENGINE AND REPORT DELIVERY

The existing GEO Engine staging service runs `codex/outreach-durable-research` at `496c0fafb0f75abb7bf86db7631d96999dfa76ff`; its health route is live. The durable research API code is deployed and requires an authenticated service token. Staging is not ready for a genuine research run: research execution is not enabled and no authorized search provider credentials are configured. The presence of an OpenAI key name in Render is not proof of usable or authorized real-provider execution. No fixture, health response, or HTTP 200 is counted as completed research.

Report generation and delivery orchestration are implemented locally, but no real consented research result or genuine report has been produced. Unattended report email is blocked because no authorized sender/mailbox provider and permissions are available. Actual sending remains disabled and unverified.

## Remaining end-to-end blockers

1. Enable an independent, automatic real-company identity verification capability for the response form.
2. Configure an authorized search provider and explicitly enable the GEO Engine's real research execution.
3. Provide a durable authorized Google Sheets scheduler without provisioning an unapproved paid service.
4. Authorize an unattended report-email provider and mailbox permissions.
5. Continue the bounded Invitation V1 backfill for eligible candidates as operational capacity allows; do not run full GEO research before invitation and valid consent.

The invitation handoff and one-action package generation work against live Neon data. Automatic consent, real GEO research, genuine report generation, and report delivery are not yet end-to-end verified.
