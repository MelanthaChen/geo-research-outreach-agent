# Phase 6C CSV and Mail Merge Preparation

## Outcome

The reproducible export command is:

```sh
uv run python scripts/phase6c_generate_exports.py
```

It reads the active Phase 6A company snapshot and campaign sidecar read-only, writes the complete cohort export, two synthetic formatting-test rows, and a full real-company eligibility audit under `exports/`, and records export metadata in a separate local ledger at `~/.local/share/geo-research-outreach-agent/phase6c_export_tracking.sqlite3`. The ledger uses `EXPORTED_PREPARED`; it does not update campaign status or mark a message Sent.

Generated against the current active snapshot:

- `exports/phase6c_all_companies.csv`: 537 rows, one per unique company.
- `exports/phase6c_test_mail_merge.csv`: exactly 2 synthetic rows for `yuxaun.chen.1031@outlook.com` and `yuxuan.chen.1031@outlook.com`.
- `exports/phase6c_real_campaign_eligibility.csv`: 537 company assessments.
- A real-company ready-to-send mail-merge CSV was not generated: zero companies passed every gate.

All files use UTF-8 with BOM, stable columns/order, RFC CSV quoting, multiline body support, and spreadsheet-formula escaping. Repeated generation produced identical content hashes. The test rows use synthetic company names and the reserved `.invalid` participation URL, explicitly identified as non-working. They are not imported into the company database and do not replace real contact addresses.

## Current real-company blockers

The assessment uses overlapping blocker counts (a company may be counted under multiple reasons):

| Blocker | Companies |
| --- | ---: |
| Company review not approved | 537 |
| Valid public HTTPS participation form not configured | 537 |
| Company invitation token unavailable | 537 |
| No suitable first-party email | 384 |
| Website identity not verified | 89 |
| Eligibility not `ELIGIBLE` | 51 |
| Contact review not approved | 153 |
| Already present in the selected campaign (dedupe protection) | 262 |

The cohort currently has pending company review for all 537 records and pending contact review for its stored contacts. The configured participation URL remains unset; the `.invalid` URL used by local drafts is never accepted as a live form. There is no private company invitation-token file. The separate `data/phase5d_campaigns.db` contains test-only campaign history; it was read but not updated by these exports.

The real eligibility audit reports each of these blockers per company. The selected-company dashboard preview may show an existing first-party contact and a draft, but the draft is explicitly `PREVIEW ONLY — NOT READY TO SEND` and its link remains a `.invalid` placeholder. It cannot be exported as a real mail merge.

## Dashboard workflow

On Companies, select companies using their checkboxes. On Campaigns, choose a campaign and preview the selected records. The preview reports all independent exclusion reasons and displays personalized draft text only for verified, eligible companies with a suitable stored first-party email. `Export Test Mail Merge CSV` downloads the two-row synthetic file. `Export Selected Mail Merge CSV` returns a CSV only if all live-preparation checks pass; otherwise it returns the exclusion reasons and creates no real mail-merge file.

The production gates are: verified website identity; eligible company and explicit company approval; first-party email and evidence URL; explicit contact approval; no address in the suppression list; no explicit opt-out; no existing item in the selected campaign; a valid HTTPS participation form URL outside `.invalid`; and a private company-specific invitation token. Company contact forms are never included in an email mail merge. Duplicate recipient addresses are excluded.

Invitation tokens, when a live form is later configured, must be supplied in a private JSON mapping at `~/.local/share/geo-research-outreach-agent/live_invitation_tokens.json`, keyed by stable company ID and containing a token of at least 32 characters. That file is not created by this workflow and is never exported by the all-company CSV. The form endpoint and token semantics must be deployed and validated before any ready-to-send CSV can be produced.

## Safety and verification

This phase added no email provider, Gmail/API call, OAuth flow, SMTP transport, message send, external form submission, company-data write, campaign-item write, or human-review update. Test export rows are synthetic formatting fixtures only. All-company and eligibility report generation use read-only cohort/campaign connections; campaign export tracking is isolated in the separate local ledger.

Validation completed: full test suite passed; dashboard JavaScript syntax check passed; `git diff --check` passed; row counts and unique company IDs were checked; test recipient list and exactly-two-row count were checked; UTF-8 BOM and multiline email body were checked; repeated generation had stable hashes; frozen 77-company database, active cohort database, and campaign sidecar hashes were unchanged.
