# Google Forms consent integration

## Current response contract

The existing form and linked response spreadsheet are retained. The current
form contract is six required questions, in this order:

1. `Invitation Reference` — short answer, prefilled with `entry.1773870597`.
2. `Is your company interested in participating in this research?` — Yes/No.
3. `Does your company authorize our research team to conduct the research described above?` — Yes/No.
4. `Full Name of the Authorized Company Representative` — short answer.
5. `Job Title of the Authorized Company Representative` — short answer.
6. `Business Email Address of the Authorized Company Representative` — short answer with email validation.

The response adapter stores interest and authorization independently. Interest
is never treated as authorization. The representative name, title, email,
invitation reference, timestamp, source, and identity-review state are retained
in `questionnaire_submissions`.

Legacy four-column exports remain readable. They map the old combined consent
answer to research interest and deliberately leave authorization as
`NEEDS_REVIEW`.

Decision states:

- YES / YES → `NEEDS_REVIEW`; no automatic consent or GEO handoff.
- YES / NO → `INTERESTED_NOT_AUTHORIZED`.
- NO / NO → `DECLINED`.
- NO / YES, missing, invalid, duplicate, or identity/email concerns → `NEEDS_REVIEW`.

The invitation recipient email remains separate from the representative email
reported by the form. A difference is preserved and flagged for review; it does
not silently invalidate the invitation reference.

## Live form preparation status

The existing form was edited in place and remains unpublished. The editor
question IDs observed after the update are `hj99tb2` (Invitation Reference),
`hj99tb6` (interest), `hj99tb10` (authorization), `hj99tb28` (representative
name), `hj99tb36` (job title), and `hj99tb38` (representative email). The
stable prefill entry remains `entry.1773870597`; the other public entry IDs are
owned by Google and should be confirmed from the response sheet after the form
is published rather than inferred from editor DOM IDs.

The response spreadsheet will use these exact column headers:

`Timestamp`, `Invitation Reference`, `Is your company interested in
participating in this research?`, `Does your company authorize our research
team to conduct the research described above?`, `Full Name of the Authorized
Company Representative`, `Job Title of the Authorized Company Representative`,
and `Business Email Address of the Authorized Company Representative`.

No form publication, response submission, email delivery, or GEO Engine action
was performed during this update.

Date: 2026-10-22

## Form finalization

The existing form was edited in the authenticated Google Forms editor without deleting the linked response spreadsheet or historical responses. The live form now exposes exactly four required questions:

1. Invitation Reference
2. Your Name and Role
3. Business Email Address
4. Research Participation & Consent — Yes / No

The original research description and a consolidated authorization/consent statement remain in the form. The old research-interest, separate authorization, follow-up-contact, website/business, and comments questions were removed. The empty legacy section was merged into the first section.

Verified form metadata:

- Form editor: `1JaUn431g9KzjmSBool8WzvCK_bjQ-EeTVteJa4oU4_4`
- Response spreadsheet: `1CqJ3abpAci7hBbGYULNpyvkjkmjn6E8uyOoSrUMOHps`
- Public form URL: `https://docs.google.com/forms/d/e/1FAIpQLSe_a-Dg-qahIGMNfnjH8x3kYfn_R_e9lFaO4_fQPBIPIfLq-w/viewform`
- Invitation Reference entry ID: `entry.1773870597`

The entry ID was read from the form's actual prefill DOM metadata and verified after the final form cleanup.

## Outreach Agent integration

`src/outreach_agent/google_forms_integration.py` provides:

- unique `GEO-...` invitation references;
- prefilled URLs using `entry.1773870597`;
- normalized Sheets-row parsing for the four form fields;
- an authenticated Google Sheets reader interface;
- idempotent batch processing counters.

The registry now stores `invitation_reference`, response source, and the existing review/consent lifecycle fields. Combined screening drafts replace both `{{questionnaire_url}}` and the legacy `{QUESTIONNAIRE_URL}` placeholder with the Google Forms prefilled URL. The invitation reference is a correlation identifier, not a secret.

Google Sheets rows are matched to an invitation reference and checked against the invitation's intended recipient email. A known reference with a missing or mismatched recipient remains `NEEDS_REVIEW`; a Yes response becomes `PENDING_VERIFICATION`, not consent. A No response records `DECLINED`. Unknown references are rejected without creating a submission. Duplicate rows are idempotent by invitation and row hash. GEO handoff remains blocked until the existing verified-consent and respondent-authorization gates are satisfied.

## Authorization and manual setup

The repository does not claim live synchronization. Before enabling it, configure an authenticated Google Sheets API service with read access to the response spreadsheet and the Sheets read scope, then call `authenticated_sheets_reader(...)` followed by `ingest_google_form_response(...)`. OAuth/service-account credential acquisition is intentionally outside the adapter and must be supplied by deployment configuration. No credentials are stored in the repository.

The form remains unpublished pending owner approval. No response was submitted, no email was sent, and no production GEO analysis was triggered during this work.

## Sheets service-account setup and one-shot verification

Required API scope:

`https://www.googleapis.com/auth/spreadsheets.readonly`

The Sheets API scope is sensitive but read-only. The service account needs no broad Google Drive role. Share only the response spreadsheet with the service-account email as **Viewer**. Do not share the form editor or other Drive files.

User-authorized setup steps:

1. In a Google Cloud project you control, enable the Google Sheets API.
2. Create or select a user-managed service account.
3. Create a key only if the runtime cannot use a short-lived attached identity or workload identity. Store the JSON outside this repository with restrictive filesystem permissions; do not paste or commit it.
4. Share spreadsheet `1CqJ3abpAci7hBbGYULNpyvkjkmjn6E8uyOoSrUMOHps` with the service-account email as Viewer.
5. For local development, prefer keyless ADC by impersonating the dedicated
   service account. The sync command also retains the JSON-key path for
   deployments that cannot use ADC.

Keyless local setup (the operator completes Google sign-in in the browser):

```bash
gcloud auth application-default login \
  --impersonate-service-account=geo-sheets-sync@boreal-pride-420500.iam.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/spreadsheets.readonly
```

Run the local verification command with ADC:

```bash
uv sync --extra gmail
PYTHONPATH=src uv run python scripts/sync_google_form_responses.py --adc --dry-run
```

The legacy JSON-key path remains supported:

```bash
GEO_RESEARCH_SERVICE_ACCOUNT_FILE=/secure/path/service-account.json \
  PYTHONPATH=src uv run python scripts/sync_google_form_responses.py --dry-run
```

Expected successful output is counts only, such as `read_ok rows=0 dry_run=true`; response contents and credentials are never printed. A real synchronization run is:

```bash
GEO_RESEARCH_SERVICE_ACCOUNT_FILE=/secure/path/service-account.json \
  PYTHONPATH=src uv run python scripts/sync_google_form_responses.py
```

This command is one-shot and read-only against Google. The local write is limited to idempotent response records and lifecycle statuses. Scheduling is intentionally not installed yet; after a successful connection, invoke the same command from an operator-owned launchd/cron/container schedule with the credential supplied through a secret manager or workload identity. Each run reports `seen`, `processed`, `duplicates`, and `needs_review`, and exits nonzero with a concise error on authentication, sharing, range, or API failures.

Google recommends avoiding long-lived service-account keys where short-lived service-account credentials or workload identity are available. See the [Sheets API scope guidance](https://developers.google.com/workspace/sheets/api/scopes) and [service-account key guidance](https://cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys).
