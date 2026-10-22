# GEO Research Outreach pipeline implementation

Date: 2026-10-22

## Implemented and operational locally

- Existing lifecycle registry remains authoritative for research records,
  recruitment state, consent, handoff, and screening results.
- The revised full-population queue contains 5,077 US companies and 285 US
  hotel properties. Company size is preserved as a soft preference only.
- Existing Research Queue pagination, search, geographic/cohort filters,
  detail inspection, selection, draft/invitation/approval controls, and CSV
  export remain connected to the registry.
- A configurable OpenAI-compatible preliminary screening adapter now exists in
  `src/outreach_agent/screening_provider.py`. It uses only supplied public-page
  evidence, requires explicit approval configuration, records model/prompt and
  token usage, and rejects unsupported ratings.
- Sender-account settings are now registry-backed and exposed through the
  dashboard settings page and `/api/sender-accounts`. They store sender
  metadata, reply-to, signature, default selection, connection status, and
  reply-sync status without storing OAuth tokens in the registry.
- The complete CSV/XLSX screening exports include identity, geography, contact
  provenance, soft size preference, eligibility, blockers, screening, and cost
  fields.

## Implemented but disconnected or blocked

- AI screening is paused at `PAUSED_AT_AI_BOUNDARY`. No approved provider,
  model, pricing, or authorization is configured. The pending workload is
  5,362 screenings, estimated at 13,405,000 input tokens and 4,825,800 output
  tokens using the worker's planning assumptions. Dollar cost is unavailable
  until pricing is configured.
- Gmail production OAuth and recruitment delivery remain disabled. The sender
  account UI is configuration-only until administrator-approved Google OAuth
  credentials and a public HTTPS deployment are supplied.
- The existing simulated send path remains clearly marked simulated and cannot
  send company email.
- GEO Engine deep audits, experiments, and monitoring remain downstream of
  verified consent and are not duplicated here.

## Safety boundaries

No real email, external form submission, paid AI call, deployment, GEO Engine
experiment, commit, or push was performed. Existing candidate stores and hotel
crawler checkpoints were not modified.

## Verification

```text
PYTHONPATH=src uv run pytest -q tests/test_full_screening.py tests/test_screening_provider.py tests/test_sender_accounts.py tests/test_research_lifecycle.py tests/test_integration_phase1.py tests/test_reporting.py
21 passed

PYTHONPATH=src uv run pytest -q
full suite passed, exit code 0
```
