# Phase 6B: Email E2E Readiness and Full Company Export

> Historical readiness report. The sender/provider configuration below was
> superseded by Phase 6B.2's Gmail API integration. The CSV export remains
> current. See [Phase 6B.2](phase6b2_gmail_test_workflow.md) for the current
> sender, OAuth setup, and form blocker.

## Outcome

The full-cohort company CSV export is implemented on the Companies page and at
`GET /api/companies/export.csv`. It is generated from the read-only experimental
cohort snapshot, sorted by company name then stable company ID, and returns one
row per company. “Download all companies CSV” ignores current filters;
“Download filtered CSV” applies the active search and filters. Multiple source
records and contact channels are represented as sorted ` | `-separated values
inside their corresponding cells, so they do not multiply company rows. Cells
that could be interpreted as spreadsheet formulas are prefixed with an
apostrophe. The file is UTF-8 with BOM for spreadsheet compatibility.

The real-email end-to-end test is **not ready and was not attempted**. The
existing delivery model is intentionally local/test-only: campaigns have a
database constraint `demo_only=1`, the dashboard has no delivery provider or
live-send endpoint, and the campaign sidecar records simulated events only.
The participation form is a local management workflow with a reserved
`.invalid` invitation URL, not a public HTTPS participant endpoint. No email
was sent, no external form was exposed or submitted, and no campaign/company
records were changed for a live test.

## Current safeguards and test addresses

The only addresses authorized by this phase brief for a future test are exactly
`yuxaun.chen.1031@outlook.com` and `yuxuan.chen.1031@outlook.com`; their spelling
is intentionally preserved and they are stored in `config/test_email.yaml`.
They are not wired into a live-send route. The sender stated in the brief is
`chen.yuxuan9@northeastern.edu`; it is stored as the preferred sender but
remains unverified and is not configured as an authenticated sender. Discovered
company contacts remain out of scope for live testing. See the
[Phase 6B.1 configuration note](phase6b1_test_email_configuration.md).

## Exact external prerequisites to unblock the E2E test

1. Northeastern IT must confirm an approved delegated Microsoft Graph
   application/consent path that permits `Mail.Send` as the stated sender (and
   a sender identity check). The repository has no tenant/client registration,
   OAuth authorization, or token vault configured. No SMTP fallback should be
   used.
2. An approved public HTTPS deployment for the participant form is required.
   It must expose only the narrowly scoped tokenized form, not the dashboard or
   campaign APIs. The current `.invalid` link is deliberately non-routable.
3. After provider and form setup, the researcher must review and confirm the
   exact recipient and exact rendered message before each send. A provider
   acceptance event must not be described as confirmed inbox delivery.

Until those requirements are met, real email stays disabled. Once configured,
the external smoke test must use a synthetic test company isolated from the
research cohort and one of the two exact allowlisted recipients. It must not
modify discovered-company responses or imply formal consent.

## Validation

Offline tests cover stable all-company and filtered export, stable schema and
ordering, formula-injection protection, and export of more than 5,000 rows.
Live provider/OAuth, actual email receipt, public form access, and a real form
submission remain unverified because the necessary university and deployment
configuration is not present.
