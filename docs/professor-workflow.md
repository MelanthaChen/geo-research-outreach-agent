# Professor workflow: implemented behavior and release state

This document reflects the implementation on `codex/outreach-ai-backfill-release`. The working application is still the previously deployed commit until the release is separately authorized and deployed. Local tests do not establish production database or Google configuration state.

## Invitation and report email templates

Invitation and report-delivery templates are stored as different `template_kind` values and have separate version sequences and activation states. An ADMIN saves an immutable draft, edits by loading a saved version and saving a new version, previews it in the application, then explicitly activates it. Only an active INVITATION template is used for later invitation generation; report delivery selects only the active REPORT template. The preview uses a `PREVIEW-NOT-SENDABLE` reference and creates no invitation, batch, consent, or message.

Invitation templates validate supported variables, a company name in the subject, required consent and questionnaire copy, a valid form URL placeholder, researcher identity, sender display name, sender address, and reply-to. The active questionnaire must remain the existing configured Form and reference field. Sender name/address/reply-to, researcher, affiliation, and template version are copied to a new invitation and its package at generation time. Activating a later version leaves prior invitation rows and stored packages unchanged. For Word/Outlook Mail Merge, the selected Outlook mailbox profile controls the actual From display name; the packaged sender name is an explicit identity check for the operator, not a mailbox API override. Invitation sending is not automated.

Report templates are separate, support their own validated placeholders, and are applied only after exact DOCX approval. The Graph adapter uses the authorized report mailbox and can set Reply-To. Template configuration alone does not authorize or perform delivery.

## Professor/admin access and invitation review

An ADMIN can create a one-time, 48-hour APPROVER invitation. The application stores only a hash of the bearer token, returns the token once for delivery through an already authorized channel, and audits issuance and acceptance. Acceptance sets the invitee-chosen password; the application does not invent an account, email address, or password. APPROVER can inspect candidates, view complete invitation batches, approve the exact package hash, and review exact DOCX versions. ADMIN-only account management is denied to APPROVER. The professor's actual email is not present in the checked project documentation, so enrollment remains pending that address.

Invitation package download requires server-side ADMIN/APPROVER review bound to the current package SHA-256. The server rechecks each invitation's current quality status and current validator before approval. Download does not send. Existing invitation content and saved package bytes remain immutable when templates change.

## Google Forms and identity

The worker reads the existing response Sheet and existing Form. It requires the independent YES research-interest and YES research-authorization answers, authority affirmation, exact invitation reference, business identity and website scope. Old responses before the recorded cutoff are excluded from new operational consent. Self-entered email is not treated as verified identity. The Forms API path requires `emailCollectionType=VERIFIED`, a Forms API `respondentEmail`, and a match to the original invitation recipient; duplicate or conflicting answers fail closed. Withdrawal/suppression blocks research.

The last verified operational state recorded by the project was `DO_NOT_COLLECT`; the local environment has no Google service-account credential and no Forms owner session. Consequently ordinary respondents remain blocked. The Forms API integration is already written to fail closed; the owner must change the existing Form's email collection setting to verified and confirm the response Sheet headers/questions. A verified Google account need not be Northeastern-affiliated, but a respondent without a Google identity cannot use this native verification path. An alternate mailbox challenge would require an authorized unattended sender, which is not configured. No Form, response, or submission was modified by this release.

## Fixed 100-company preparation cohort

The cohort selector uses existing V2 records only. It requires `kind=company`, a non-fixture record, verified official website, ELIGIBLE/QUALIFIED status, no suppression, no existing invitation for that candidate, domain, or recipient, and exactly one valid contact with `verification=VERIFIED`, a same-site source URL, supporting contact evidence, and no suppression. It rejects duplicate website domains and recipients, and round-robins through industries. The saved 100 membership IDs and per-member evidence snapshot are immutable; the authenticated UI can review and download CSV with candidate ID, company, industry/location, website, business contact, verification evidence, source references/checksums, personalization state, and review state. It never silently replaces members.

Cohort preparation does not personalize, create an invitation, authorize consent, research a company, or send a message. Personalization is reported as PENDING unless a current V2 analysis already exists. The stored candidate size evidence is not a uniform firmographic source, so the snapshot says `NOT_ESTABLISHED_IN_STORED_RECORD`; the selector does not label companies as SMBs without evidence. No live cohort was created in this local work: the current execution environment has neither `OUTREACH_V2_DATABASE_URL` nor `OUTREACH_DATABASE_URL`, and direct Neon reads were therefore unavailable. The code is tested against disposable SQLite records only. Production count and IDs remain unverified.

## DOCX review and delivery

The V2 report pipeline preserves the generated DOCX, original research result provenance, recipient, and SHA-256. ADMIN/APPROVER can download and inspect it, then record APPROVED or CHANGES_REQUESTED against the exact current hash. The worker checks current consent, recipient, document bytes/hash, latest decision, and active reviewer identity before constructing the email provider. A modified document requires a new decision. Research completion only prepares the report; it does not approve or send it.

The older local registry workflow is also fail-closed: its resume scheduler leaves completed reports at `PENDING_REPORT_APPROVAL`, and its explicit report-delivery function requires a matching immutable approval. The dispatcher independently verifies the exact document hash, content binding, and original recipient before using a fake or real adapter. Neither scheduler path can send an unreviewed or changed DOCX. Actual delivery remains blocked until a legitimate unattended mailbox and tenant permissions are configured.

## Migration and test boundary

Migration 7 adds `email_templates.template_kind` (defaulting existing template rows to `REPORT`), invitation-template identity fields, and `invitations.sender_email`. Migration 8 adds nullable `sender_name` columns to `email_templates` and `invitations`. Both are additive; they do not rewrite invitation content, consents, jobs, research results, reports, or source candidates. Existing rows preserve their values; new columns are nullable/defaulted. App startup applies migrations transactionally with a PostgreSQL migration-table lock. The migration was tested against a disposable SQLite database containing v6-style populated invitation and template rows; it has not been applied to Neon.

Rollback is code-only: redeploy the prior application commit and retain the additive columns and migration records. Do not drop columns or restore over later data. Before deployment, take/verify the existing Neon backup, record v6 counts, deploy the authorized release once, verify migration rows through v8 and counts, then query an existing invitation/template/report/consent/job sample. If verification fails, roll back application code without reversing the additive schema.

## Verified local state and remaining external inputs

- Local implementation is on `codex/outreach-ai-backfill-release`; no production deployment, OpenAI call, invitation send, or GEO research occurred.
- Recurring GPT personalization remains disabled by default in `render.yaml` and `worker.tick()`; the five-company paid pilot was not run.
- The professor's actual email is missing; it is required to issue the APPROVER invite.
- The existing live Form identity setting must be changed from `DO_NOT_COLLECT` to verified respondent email by the Form owner; no local Google credential was available to perform or verify that setting.
- The current shell has no authorized Neon URL, so no live cohort, live migration verification, or live data-count audit was performed.
- The authorized unattended Microsoft sender and tenant application permissions remain absent; no report delivery occurred.
- The feature branch may be pushed for release review, but the existing release hold remains: no Render deployment without the separately required deployment authorization.
