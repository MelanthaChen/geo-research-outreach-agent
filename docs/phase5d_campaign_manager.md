# Phase 5D — Persistent Campaign Manager and Participation Interest Demo

## Scope and safety boundary

Phase 5D adds a campaign workspace to the existing local professor dashboard. It reads the 537-company Phase 4F cohort, qualifications, saved identity-verification exports, contact evidence, and existing human-review values. Campaigns, drafts, simulation events, demo form tokens, synthetic submissions, and audit history are stored in a separate SQLite sidecar (`data/phase5d_campaigns.db`). It does not add tables to or write to either company database.

The dashboard remains loopback-only. It has no email/SMTP/Gmail/Microsoft provider, no real-send endpoint, no external company-site requests, no public participation form, and no website-form submission path. The “form” is an administrator-triggered local demo in the dashboard; the visible token URL uses the reserved `.invalid` domain. All campaign approval, delivery, response, form-open, submission, review, and follow-up events are labeled DEMO or SIMULATED and isolated from actual company/contact decisions. A simulated interest is not formal consent, IRB approval, or a commitment to participate.

Sender, contact, researcher, team, university, study-description, privacy-notice, and real signup configuration remain unset in `config/outreach.yaml`. The reserved `.invalid` URL is for display only. Contact-form channels are assigned to a distinct manual review queue, never submitted. Saved contact review status is shown but never modified by campaign approval.

## Campaign workflow

1. Search/filter the existing company cohort by source, identity, eligibility, GEO opportunity, priority, channel availability, and campaign-history status.
2. Create a named campaign from *all* current matches. Selection uses stable company IDs; the count is not limited to the 250 visible list rows.
3. Review selected, eligible, unresolved, excluded, contact-form-review, and draft counts. Only verified identities and eligible companies with a suitable first-party channel get a draft. No-channel, unverified, ineligible, suppressed, previously campaigned, or draft-validation records remain excluded with a visible reason.
4. Business email is preferred when a company has both email and a form. Email drafts are saved as NOT SENT. Form-only drafts are held in `CONTACT_FORM_REVIEW` and are not in the email delivery queue.
5. Explicitly approve the campaign for DEMO simulation. This is not a real outreach authorization and does not write company or channel review fields.
6. Simulate batches of at most 10 items. Each attempt is auditable, idempotent, persisted, and resumable; a campaign has a 50-event UTC-day demo cap. Fixture failures can be retried up to two attempts. Pause, resume, cancel/token revocation, per-campaign emergency stop, and global emergency stop are available. Clearing the global stop leaves campaigns paused; it never resumes them automatically.
7. Record explicitly synthetic recipient responses and, when interested, use the local-only simulated participation form. It resolves the opaque random token to its campaign/company; it does not infer identity from submitted text. Duplicate submissions are ignored, tokens expire after 30 days and can be revoked, and each submission starts pending research-team review. Demo review and follow-up are explicit sidecar events only.

No opens, click-through rates, or real response rates are claimed. `form_opened_at` is an explicitly simulated event, not web analytics. Analytics are database-derived and show demo versus real counts separately; real sends, real form submissions, and formal consents remain zero.

The existing cohort filter has campaign-history values: `NO_CAMPAIGN_HISTORY`, `DEMO_CAMPAIGNED`, and `SIMULATED_DELIVERED`. The explorer displays 50 companies per page while campaign creation resolves every matching filter across the full cohort (up to a hard limit of 5,000 IDs). Campaigns cannot add a second invitation for a company already held in an active campaign or with a prior simulated delivery. The configured suppression CSV is read-only during campaign preparation; matching email recipients are excluded. The normal 10-item batch and 50-attempt daily ceilings apply only to this local simulator—real delivery is disabled altogether.

## Run and reproduce

From the repository root:

```sh
# Seed or resume the persistent, labeled 20-company professor scenario and write the CSV exports.
uv run python scripts/phase5d_campaign_demo.py seed --count 20

# Re-export from the current persistent state. Repeating this command is byte-stable.
uv run python scripts/phase5d_campaign_demo.py export

# Start the dashboard (no API keys or provider credentials required).
uv run python scripts/phase5b_dashboard.py
```

Open `http://127.0.0.1:8765`. The scenario command is idempotent: it resumes unfinished batches and does not append additional synthetic responses after its three example response states are present. It creates a deterministic 20-company selection from the current saved cohort, including one form-only case if available. Random opaque tokens and timestamps are intentionally generated once and then persist with the campaign; repeating `export` does not regenerate them.

To reset one demo campaign, use the exact campaign ID printed by `seed`. This deletes only that DEMO campaign and its sidecar queue, tokens, submissions, and audit rows. It does not modify the cohort, exports unrelated records, or reset human review:

```sh
uv run python scripts/phase5d_campaign_demo.py reset --campaign-id <campaign-id>
```

The two generated exports are:

- `data/phase5d_demo_campaign_summary.csv` — one database-derived summary row per saved campaign.
- `data/phase5d_demo_outreach_status.csv` — stable company/campaign rows, saved evidence references, draft/queue/delivery/response/submission states, token expiration, and explicit `source_data_mutated=NO`.

Tokens and form data are only in the local ignored SQLite sidecar and the local-only dashboard response. Do not publish the sidecar or copy live personal information into synthetic demo fields.

## Scaling and future production requirements

The company explorer filters and paginates visually while campaign selection is performed against the full filtered cohort. Campaign items have stable IDs, a unique campaign/company constraint, unique idempotency keys, bounded batches, retry counts, persisted queue states, and indexes for company history and delivery queue reads. For thousands of companies, measure SQLite query latency and export size with representative data before changing the storage engine or batch ceilings; this phase does not claim production throughput or 5,000-company delivery capacity.

Before any real outreach or publicly reachable form is considered, the research team must select/authorize sender and reply-to identities, configure a real form and approved privacy text, complete university/IRB and data-retention review, establish authentication/authorization and abuse protections, approve suppression/opt-out handling, define rate limits and daily provider caps, and explicitly authorize a provider integration. None of those production paths are implemented here.
