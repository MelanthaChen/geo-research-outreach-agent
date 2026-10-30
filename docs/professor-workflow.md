# Outreach research lifecycle and release gates

This document describes the existing Outreach V2 workflow and its current external dependencies. “Prepared” and “approved” are workflow states; neither means an invitation was sent or research was run.

## Invitation preparation and review

1. An authenticated operator selects eligible companies. Selection requires the current GPT semantic personalization version, a passing quality result, verified website status, one eligible first-party business email, no suppression, and no earlier invitation.
2. Outreach renders each invitation using the active versioned `INVITATION` template (or the built-in V1 template when no custom version is configured), the active questionnaire mapping, researcher identity, and the exact personalization evidence. The Word Mail Merge keeps the authorized shared bulk subject.
3. The application persists a batch and its invitations as `PENDING_REVIEW`. The initial API response contains only batch metadata; the package download endpoint refuses access until approval.
4. An ADMIN, APPROVER, or OPERATOR can inspect the complete subject, body, recipient, website, evidence, quality result, and exclusions. An ADMIN or APPROVER can approve the exact package SHA-256. The server rechecks each current quality gate and records reviewer identity, timestamp, hash, and an audit event.
5. Only an approved package is downloadable. Downloading does not send email. Actual invitation delivery remains operator-controlled through Word/Outlook; no automatic invitation sender is configured.

## Google Forms, consent, and research

The existing invitation reference remains embedded in the existing Google Form URL and is matched to the original invitation. Sheets headers are canonicalized, duplicate conflicts fail closed, and old response rows are retained as historical source data rather than imported as new consent.

The worker accepts independent interest and research-authorization answers, representative identity, authority affirmation, timestamp, and exact invitation scope. A self-reported email alone does not establish identity. A Form API respondent account collected as `VERIFIED` must match the original invitation recipient. The current live Form setting `DO_NOT_COLLECT` therefore blocks ordinary-company identity verification; the Form owner must enable verified respondent-email collection before ordinary submissions can authorize research. Exact owner-controlled bindings remain narrowly scoped to their existing fixtures.

Only a validated YES + YES submission creates the durable, idempotent research job. Consent processing does not itself run research. Outreach research execution and GEO Engine execution are separate switches and provider configurations; the personalization pilot does not invoke the consent, research, report, or delivery worker stages.

## Research report review and delivery

After a consented job completes, Outreach validates scope and result provenance, generates a DOCX, stores its bytes and SHA-256, and associates it with the originating job and recipient. Delivery checks the actual document hash and requires approval of that exact hash by an active ADMIN or APPROVER before constructing the mail provider. Changes or a newer report hash require a new review. Approval does not itself authorize any unrelated research action.

## Professor access

An ADMIN can create a one-time, expiring role invitation with role `APPROVER`. The invitation endpoint does not email the bearer token; it returns a one-time setup token for the operator to transmit through an authorized channel. Acceptance activates the account with a password chosen by the invitee. Invite creation and acceptance are audited without logging the token. The professor's actual email address is still required before issuing this invitation; no account is fabricated.

## 100-company cohort

The fixed cohort selector takes only current, non-fixture, quality-passed, invitation-ready companies, excludes duplicate website domains and recipients, and round-robins across available industries. Cohort creation stores eligibility snapshots and creates no invitations. The cohort invitation package is generated only from its saved member IDs and must pass exact-package review before download. GPT analysis for companies beyond the authorized five-company pilot requires a separate spend authorization; cohort preparation does not imply model processing or sending.

## Current verified limitations

- The existing five-company pilot has a user-authorized $0.25 total cap, but no paid requests may run until the replacement `OPENAI_API_KEY` is actually present in the existing Outreach Render service and the exact five-ID cohort/budget settings are configured. The current Render Environment page was inspected by variable name only and did not list `OPENAI_API_KEY`; no secret value was opened or copied. No pilot request was made.
- No invitation emails or research reports were sent as part of local implementation or tests.
- The live Form remains `DO_NOT_COLLECT`, so ordinary respondent identity is not independently verified.
- The professor's email is not known, so no professor account can be invited yet.
- New local additive schema migration 7 adds invitation template kind/identity columns. It has only been exercised in disposable SQLite tests; it has not been applied to Neon.
- Local release changes have not been pushed or deployed. Existing user instruction requires a fresh `DEPLOY NOW` before any subsequent push or deployment.
