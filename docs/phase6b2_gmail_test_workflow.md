# Phase 6B.2: Gmail Test Email Workflow

## Implemented workflow

- Settings stores/displays the preferred sender `yuxuan.chen.1031@gmail.com`
  and the exact recipients `yuxaun.chen.1031@outlook.com` and
  `yuxuan.chen.1031@outlook.com`.
- Campaigns provides a recipient selector, full test invitation preview
  (sender, recipient, subject, body, and participation-form URL), explicit
  review checkbox, Send Test Email action, and recent provider-event history.
- Backend delivery revalidates the recipient against a fixed two-address
  allowlist, confirms that the submitted preview hash matches the server's
  current message, requires explicit confirmation, and blocks all discovered
  company addresses.
- Gmail API success is recorded as `PROVIDER_ACCEPTED`, with the Gmail message
  ID and `delivery_confirmation=NOT_CONFIRMED`. HTTP API rejection, ambiguous
  transport failure, and pre-send configuration errors are separate outcomes.
  Identical test message/recipient pairs are idempotently blocked from duplicate
  sends. No real company campaign or company record is changed.
- Test-send events live in a separate local SQLite store at
  `~/.local/share/geo-research-outreach-agent/test-email-events.sqlite3`.
  OAuth credentials are stored in the OS keyring, never in this database or
  the repository.

## Install and authorize Gmail

1. In Google Cloud Console, select/create a project, enable the Gmail API, and
   configure the OAuth consent screen. Add the Gmail account to the test-user
   list if the consent screen is in Testing mode; complete any Google OAuth
   verification requirements applicable to the app/scopes.
2. Create an OAuth client with application type **Desktop app**. Download its
   JSON and place it outside the repository at
   `~/.config/geo-research-outreach-agent/gmail-oauth-client.json` (or set
   `GEO_OUTREACH_GOOGLE_OAUTH_CLIENT_FILE`). Restrict file permissions to the
   local user. This desktop client JSON is not an access token.
3. Install the optional integration dependencies with `uv sync --extra gmail`.
   The integration requests the Gmail sending scope
   `https://www.googleapis.com/auth/gmail.send` plus the standard OpenID/email
   identity scopes needed to verify which Google account authorized. It does
   not request Gmail read, compose, or mailbox-settings access.
4. Start the dashboard and click **Connect Gmail** in Settings. Google sign-in
   uses the system browser and a loopback callback. The app verifies the signed
   in account equals `yuxuan.chen.1031@gmail.com`; if not, it discards the
   returned tokens. A working OS keyring is required; the flow fails closed
   rather than writing token JSON to a plaintext file.

Google's desktop OAuth guidance requires a registered Desktop app client and
supports the local installed-app authorization flow. The Gmail API marks
`gmail.send` as a sensitive sending scope. See [Google's Gmail Python
quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python),
[installed-app OAuth guidance](https://developers.google.com/identity/protocols/oauth2/native-app),
[Gmail scope table](https://developers.google.com/workspace/gmail/api/auth/scopes),
and [Gmail send API guidance](https://developers.google.com/workspace/gmail/api/guides/sending).

## Secure HTTPS form prerequisite

No public form is deployed by this repository. The old `.invalid` URL is never
shown as an active link. The preview uses an explicit “PUBLIC HTTPS TEST FORM
URL NOT CONFIGURED” placeholder until a URL is entered in
`config/test_email.yaml`.

To enable a real test send, deploy a separate, narrowly scoped test form at a
public HTTPS origin. Do not expose the dashboard, SQLite stores, or management
APIs. The form service must at minimum:

- accept only opaque, high-entropy, one-time invitation tokens; persist only a
  cryptographic token hash; associate the token with a synthetic test event,
  expiry, and revocation state; and never put personal information in URLs;
- validate tokens server-side, expire/revoke them, rate-limit submissions,
  validate fields and payload size, and use HTTPS end-to-end;
- save submissions in a separate test-only store with clear TEST provenance;
  do not write a discovered company's response, human approval, or consent;
- show a clear confirmation that the submission is a workflow test and is not
  formal research consent.

After independently verifying those controls from a browser outside the
researcher's machine/network, put the public URL in
`participation_form_url` and set `participation_form_endpoint_verified: true`
in `config/test_email.yaml`. This explicit verification flag is required to
enable the send action. Merely supplying any HTTPS URL does not enable sending.
The actual form hosting/deployment remains an external prerequisite.

## Current state and limitations

At implementation time there is no OAuth desktop-client JSON, Gmail account
connection, or public HTTPS form URL configured. Therefore:

- Gmail connection status: **not connected**.
- Send Test Email: **disabled**.
- Real emails sent: **none**.
- Gmail API acceptance events: **none**.
- End-to-end email → Outlook → public form → submission → Responses/Analytics:
  **not completed**.

The Responses and Analytics pages continue to report their existing stored
campaign/response data. No public test-form callback is deployed, so the
future external form cannot yet append a test submission to those views. After
the hosting path exists, integrate submissions using a separate `LIVE_TEST`
source and never map them onto the 537-company cohort. No form-open/click event
is inferred from a send or from Gmail API acceptance.

Test sending remains an explicitly confirmed, one-recipient action. Company
campaign delivery remains disabled. The 537-company data and full/filtered
company CSV exports are unchanged.
