# GeoAIResume E2E Post-Test Review

## Scope

This review records the completed owner-controlled technical test and the
cleanup applied afterward. The invitation was not recreated or resent, and no
production research record was modified.

## Verified E2E evidence

- Sender: `chen.yuxuan9@northeastern.edu`.
- Recipient: `yuxuan.chen.1031@outlook.com`.
- Invitation reference: `GEO-6BF37FA993AF1A4D`.
- Word Mail Merge source: one approved XLSX row.
- Outlook Outbox: empty after dispatch.
- Outlook Sent Items: message present at 16:41; message-header evidence showed
  `From: chen.yuxuan9@northeastern.edu` and the approved recipient.
- Recipient Outlook inbox: the message appeared as an unread message.
- Google Form: the personalized URL loaded, displayed the prefilled invitation
  reference, and Google confirmed that one response was recorded.
- Sheets synchronization: `read_ok rows=1 dry_run=true`, followed by isolated
  registry processing with `seen=1 needs_review=1`.
- Idempotency: the second isolated synchronization returned
  `duplicates=1` and created no second submission.
- Isolated Research Queue state: `RESPONDED` questionnaire,
  `NEEDS_REVIEW` consent, matched invitation email, and zero GEO handoff
  records. The response did not become verified consent automatically.

The historical sent message rendered the requested em dash as two dashes in
Outlook. This was observed and not corrected by resending.

## Cleanup implemented

`mail_merge_safe_subject()` now normalizes common typographic punctuation to
ASCII equivalents. It is applied to combined screening invitations, dashboard
subjects, Word XLSX exports, and the isolated test preview. Future subjects use
`-`, `...`, straight quotes, and ordinary spaces where Word Mail Merge for Mac
has known punctuation-rendering risk.

The generated test invitation wording is state-independent: it describes the
message as an authorized owner-controlled workflow test and does not claim
that an email has not been sent. Existing historical artifacts were not
rewritten, and the verified invitation was not resent.

Regression coverage now checks:

- ASCII-safe subject rendering;
- personalized Google Form URLs and invitation references;
- complete combined-analysis invitation bodies without stale send claims;
- approved-only Word export behavior; and
- duplicate-safe export/reconciliation behavior.

## Batch-readiness review

The existing workflow is suitable for a controlled next stage only when each
batch is constrained by all of the following:

- recipient email and contact evidence are verified;
- company and contact review are approved;
- draft approval status is `APPROVED`;
- US geographic eligibility and consent gates remain satisfied;
- suppression and duplicate-recipient checks pass;
- the XLSX contains only the explicitly approved records;
- the operator sets a small maximum-recipient and maximum-message limit; and
- each exported record is reconciled separately using Outlook evidence.

Exporting an XLSX, opening Word, or completing a merge does not by itself
prove delivery. Reconciliation must distinguish `EXPORTED_PREPARED`,
`SUBMITTED_TO_OUTLOOK`, `SENT_CONFIRMED`, `DELIVERY_UNVERIFIED`, and provider
failure states. A send with uncertain outcome must not be retried
automatically.

## Remaining blockers before scaling

1. Outlook Sent Items confirms submission by the configured mailbox, but it is
   not a provider-level delivery receipt. Delivery and bounce reconciliation
   remain manual unless an approved provider integration is introduced.
2. Human review is still required for every batch and for questionnaire
   identity/authorization. A YES response remains `NEEDS_REVIEW` until the
   respondent's authority is verified.
3. The Word Mail Merge workflow requires a desktop operator and explicit
   sender verification. It is not unattended scheduling.
4. The current owner-controlled test demonstrated one recipient only. No
   multi-company campaign is authorized by this review.

No OAuth, Microsoft Graph, Gmail API, new campaign, GEO analysis, or production
database update was performed as part of this cleanup.
