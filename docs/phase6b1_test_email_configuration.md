# Phase 6B.1: Test Email Configuration

> Historical snapshot. Phase 6B.2 supersedes the Northeastern/Microsoft Graph
> sender described below with the configured Gmail sender and Gmail API flow.
> For current settings and prerequisites, see
> [Phase 6B.2](phase6b2_gmail_test_workflow.md).

## Persisted configuration

The preferred sender and two authorized test recipients are stored in
`config/test_email.yaml`:

- Sender: `chen.yuxuan9@northeastern.edu`
- Recipient 1: `yuxaun.chen.1031@outlook.com`
- Recipient 2: `yuxuan.chen.1031@outlook.com`

The values are intentionally distinct and are preserved exactly. Runtime
configuration loading validates the file against the backend's fixed sender
and recipient allowlist in `src/outreach_agent/test_email_settings.py`. A
configuration-file change alone cannot expand the allowlist. Saving and
displaying these addresses requires no OAuth.

## Dashboard locations

- Settings → Email Configuration displays the preferred sender, both
  authorized test recipients, provider status, send status, and the missing
  Graph authorization prerequisite.
- Campaigns → Send Test Email provides a selector for either allowlisted
  recipient and a preview of a generic synthetic test message. It does not use
  company contact data or insert a discovered company identity into the test
  message.
- The explicit “I reviewed the recipient and message” checkbox is present, but
  the send button remains disabled because the provider is unauthenticated.

## Delivery boundary

The UI send control is disabled, and `POST /api/test-email/send` independently
enforces the exact recipient allowlist. Unauthorized addresses receive HTTP
403. An allowlisted address with explicit confirmation receives HTTP 503 with
`sent=false` and `provider_accepted=false`; no event or fake success record is
written. The endpoint has no provider implementation behind it.

The exact prerequisite is: Northeastern IT must approve/register a public-client
OAuth application with delegated Microsoft Graph `Mail.Send` permission and
tenant consent; then the sender must sign in and verify the sender identity.
No account authentication, provider access, or real email send was attempted.

## Validation

Tests cover persisted exact values, preview generation, rejection of a third
recipient, fail-closed sending for an allowlisted recipient, UI configuration,
and the disabled send state. Existing full-company CSV behavior is unchanged.
