# Phase 5A: Professor Demo Walkthrough

## Safety and scope

This demonstration needs only the local Python environment and the existing Phase 4F experimental database. It requires no selected sender account, credentials, network connection, or live provider. Sender name, sender email, reply-to address, researcher/institution fields, and production research signup URL are unset in `config/outreach.yaml`; live sending is false. The program opens `data/phase4f_experimental.db` read-only and does not write to company/contact approvals. No email is sent and no website form is submitted.

The saved qualification, company, website, and contact evidence is real evidence already in the experimental cohort. Generated messages are drafts. The Vexo delivery, company response, approval fixture, and signup handoff are explicitly simulated; the `.invalid` signup link is reserved and non-operational. A simulated positive response does not represent a real company response, approval, consent, completed signup, or human decision.

## Exact command

From the repository root, with `uv` installed and dependencies already synced:

```sh
PYTHONPATH=src uv run python scripts/phase5a_outreach_demo.py demo --max-draft-companies 10
```

The command is offline and deterministic. It writes these spreadsheet-readable files under `data/`:

- `phase5a_outreach_candidates.csv` — ten suitable-channel examples plus the no-channel example, with qualification and evidence labels.
- `phase5a_email_drafts.csv` — nine draft-only business-email messages. Unconfigured sender/reply-to values are visibly marked in the message body.
- `phase5a_contact_form_drafts.csv` — one proposed field mapping for an official form; it is never submitted.
- `phase5a_delivery_simulation.csv` — one simulated delivery/response/handoff, draft-only rows, the form-not-submitted row, and the no-channel result.

## Expected output

The stable summary is:

```text
PHASE 5A PROFESSOR DEMO — SIMULATED / OFFLINE ONLY
cohort=537 draft_companies=10 email_drafts=9 form_drafts=1
simulated_deliveries=1 simulated_interested_responses=1 simulated_signup_handoffs=1
real_sends=0 form_submissions=0 sender_configured=False
```

The detailed company lines include:

```text
5    Vexo: GENERAL_BUSINESS_EMAIL — info@vexoai.com
105  AnswerThis: CONTACT_FORM — https://answerthis.io/contact-us
88   CodeWisp: NO_SUITABLE_CHANNEL — no draft
```

The ordering/spacing is aligned by the CLI. These are cohort IDs and stored evidence, not claims that the address/form was contacted or that the company approved participation. Re-running the command reproduces identical CSV exports. The already-recorded simulated delivery event is idempotently reused.

## Five-minute presentation script

**0:00–0:40 — Set the boundary.** “The sender has not been chosen, so this demo has no email credentials and no live delivery path. Everything labeled DEMO or SIMULATED is a local fixture. We will not contact a company or submit a form.” Point out the empty sender fields and `live_sending_enabled: false` in `config/outreach.yaml`.

**0:40–1:30 — Show the existing cohort.** Run the command above. Explain that the 537 companies and qualification outcomes come from the existing Phase 4F experimental cohort. The demo reuses stored, first-party channel evidence rather than inventing companies or contacts.

**1:30–2:20 — Show the email case.** Open `data/phase5a_email_drafts.csv`, filter to Vexo, and show its saved business-email evidence, qualification/identity statuses, draft, and unapproved review state. Explain that the configured recipient is an existing published channel, but there is no actual send.

**2:20–3:00 — Show the contact-form and no-channel cases.** Open `data/phase5a_contact_form_drafts.csv` for AnswerThis. Review the source URL, visible field evidence, and proposed mapping. The `NOT_SUBMITTED_DEMO` status is explicit. Then show CodeWisp in the candidates export: no suitable channel means no draft and no attempt.

**3:00–4:10 — Show the simulated lifecycle.** Open `data/phase5a_delivery_simulation.csv`. Walk through the single Vexo row: demo-only approval fixture → simulated delivery → simulated interested response → simulated signup handoff. The fixture does not modify real approval/review data. `signup_completed=false` and `research_consent_status=NOT_ESTABLISHED`; interest is not consent.

**4:10–5:00 — Close with the boundary and next decision.** “Phase 5A demonstrates preparation, evidence, review gates, and an auditable simulated lifecycle. Real delivery is deliberately a future configurable integration. Before that, we need a selected sender, institutional approval of the message and signup destination, human-reviewed company/channel approvals, and a separately reviewed provider adapter.” Invite feedback on the study invitation and reviewer workflow, not on simulated company outcomes.

## Optional offline contact-extraction continuation

This is not needed for the five-minute demo. It extracts from saved page snapshots only, in resumable batches, and updates contact status/records in the Phase 4F experimental database while checking that human review fields remain unchanged:

```sh
PYTHONPATH=src uv run python scripts/phase5a_outreach_demo.py enrich-cache --limit 100 --batch-size 25
```

It does not inspect the frozen 77-company database, crawl, invoke fallback, or use the network. Do not run it during the presentation unless demonstrating the data-preparation workflow; it can take time and intentionally changes experimental contact records.
