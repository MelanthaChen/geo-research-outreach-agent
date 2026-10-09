# Phase 5D — Five-minute professor walkthrough

## Before the meeting

From the repository root, run:

```sh
uv run python scripts/phase5d_campaign_demo.py seed --count 20
uv run python scripts/phase5b_dashboard.py
```

Open `http://127.0.0.1:8765`. The seeding command reuses the current 537-company experimental cohort and persists a DEMO-only campaign in `data/phase5d_campaigns.db`. The company and frozen 77-company databases are read-only. The dashboard has no provider credentials, external website requests, actual sending, or external form endpoint.

Expected first-run shape (exact IDs and generated token/timestamps vary):

```text
status=COMPLETED companies=20 drafts=20 simulated_deliveries=19 simulated_failures=2 simulated_submissions=1 real_sends=0 real_form_submissions=0 formal_consents=0
```

The selected companies have saved VERIFIED website identity, ELIGIBLE qualification, and at least one suitable first-party channel. There are 19 saved business-email drafts and one contact-form draft in a separate review queue. Two failures are deterministic synthetic delivery-failure events that are retried successfully; they are not provider failures. The scenario contains three synthetic recipient-response examples, one synthetic interest submission, an explicitly labeled demo research-team review, and a simulated follow-up handoff. No real recipient responded.

## Five-minute script

### 0:00–0:45 — Cohort and evidence

Point out the top cohort metrics and the large company list. Explain that qualifications, website identity, provenance, contact channels, and missing evidence are read from the existing research cohort. Search and use the filters; the “outreach history” filter is derived from the separate campaign sidecar. Selecting a company reveals source, qualification evidence, contact evidence, and saved human-review status. Machine qualification does not equal approval.

### 0:45–1:30 — Company-specific invitation preview

Select a verified eligible company with a saved business email and generate a draft. Point to the evidence-backed recipient, company-personalized salutation, participation-form call to action, unset researcher/contact configuration warnings, and visible `DEMO DRAFT — NOT SENT`. The URL is a reserved `.invalid` placeholder. Research contact email is only for study questions; interest is directed to the form. This preview is a draft and is not in itself queued or approved.

### 1:30–2:00 — No-channel and form-only cases

Use the contact-coverage filter and show a company with no usable channel: the UI explains why a draft is blocked and does not invent an address. In the campaign below, show the form-only company. Its concise message is saved in `CONTACT_FORM_REVIEW`, separate from email deliveries. No website form is ever submitted.

### 2:00–3:15 — Persistent campaign

Scroll to Campaign workspace. The existing filter values determine the full stable-ID selection, including companies beyond the currently visible rows. Open “Professor walkthrough — DEMO ONLY” and inspect the 20-company summary, separate eligible/unresolved/excluded counts, contact-form review queue, and saved email drafts. The synthetic scenario has already recorded an explicit DEMO campaign approval; this does not modify company or contact review states and is never interpreted as real authorization.

Show the simulated batch outcomes and campaign audit. The 19 completed deliveries and two recovered failure attempts are local demo records only. The 10-item batch size and 50-event daily ceiling bound this simulator; the real transport is absent. Pause/resume, cancel/token revocation, and emergency stop are local queue controls.

### 3:15–4:20 — Interest form and response lifecycle

Open the simulated interested response and its local-only participation form. The fields cover company/site, contact name/role, business email, interest choice, and optional questions. The displayed study description/privacy notice are “Not configured” until approved values are supplied. Explain that the opaque random token maps the demo form to its company/campaign without personal data in the token. The `.invalid` link cannot be a live signup URL and no public form route exists.

Submit only the labeled synthetic fixture if demonstrating interactively. Review its `PENDING_RESEARCH_TEAM_REVIEW` state, then record “DEMO research-team review” and “SIMULATED follow-up.” The workflow states clearly that interest is not consent, formal consent remains absent, and no follow-up message is sent.

### 4:20–5:00 — Analytics and safeguards

Click the campaign metric cards to filter the campaign list, inspect company-level Outreach History, then point out the CSV exports. They report saved drafts, channel review, simulated delivery/response/form events, failure attempts, and zero real sends/submissions/consents. There are no open or click analytics. Campaign records persist after refresh in a sidecar separate from both cohort databases.

## Real versus simulated

Real inputs: cohort companies, qualification and identity results, official website values, stored contact-channel evidence, source provenance, and saved human-review values. These are read-only.

Simulated outputs: DEMO campaign approval, drafts stored in the sidecar, delivery/failure attempts, recipient responses, form opens, synthetic interest form data, research-team demo review, and simulated follow-up handoff. The demo scenario explicitly uses `demo-participant@example.invalid` and `SIMULATED DEMO RESPONDENT`; these are fixture strings, not a company representative.

The dashboard never sends email, posts to a company site, navigates to a contact form, creates an external signup form, reads email, or changes a canonical approval. No one has consented in this demonstration.

## Reproduce and reset

```sh
uv run python scripts/phase5d_campaign_demo.py seed --count 20
uv run python scripts/phase5d_campaign_demo.py export
```

`seed` is resumable/idempotent; `export` can be repeated and produces byte-identical CSVs while the sidecar state is unchanged. To remove only the named DEMO campaign, use the campaign ID printed by `seed`:

```sh
uv run python scripts/phase5d_campaign_demo.py reset --campaign-id <campaign-id>
```

Resetting removes that campaign's demo records only. It does not reset the company cohort, its reviews, or any other campaign.
