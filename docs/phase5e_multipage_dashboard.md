# Phase 5E: Multi-Page Research Outreach Dashboard

The local professor dashboard now has persistent navigation for Overview, Companies, Campaigns, Interest Form, Responses, Analytics, and Settings. The views are routed with URL hashes and rendered in a shared application shell; they reuse the Phase 5D local API, 537-company read-only cohort, saved website/contact evidence, and isolated campaign SQLite sidecar.

## Start locally

```bash
uv run python scripts/phase5b_dashboard.py --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. Navigation can also be linked directly with `/#overview`, `/#companies`, `/#campaigns`, `/#interest-form`, `/#responses`, `/#analytics`, and `/#settings`.

## Page guide

- **Overview** summarizes saved cohort metrics and recent campaigns.
- **Companies** provides existing search/filter/pagination, evidence details, multi-selection across pages, and the existing invitation draft preview. “Select all matching filters” loads IDs matching the current filters, not just the visible page; selection stays in browser memory while navigating.
- **Campaigns** creates campaigns from selected IDs, or—when none are selected—from all companies matching the active filters. Existing draft review, test approval, bounded test batches, pauses, cancellation, emergency stops, separate contact-form queue, audit history, and synthetic response actions remain in place.
- **Interest Form** describes the unified form and allows a local test submission using an unexpired token from a test-delivered campaign recipient. It records a local form-open event, then submits only to `/api/demo/interest`; submission is not consent.
- **Responses** searches saved response/interest records and labels every record as test data. A campaign invitation alone is not listed as a response.
- **Analytics** separates saved qualification breakdowns from campaign test-delivery/failure/response/submission counts and displays real sends independently (currently zero).
- **Settings** shows configuration values and explicitly identifies unset values and Test Transport.

## Data and safety boundaries

Company qualification, identity evidence, contacts, and human-review decisions are loaded from the existing cohort and are not edited. Campaigns, invitation drafts, test delivery events, test tokens, test responses, submissions, and audit history remain in `data/phase5d_campaigns.db`, separate from both the experimental company database and the frozen 77-company database. Settings are informational; the dashboard does not accept credentials or enable a live provider.

The server binds to loopback. Frontend requests are same-origin only. No public participation form route, external company website request, mail provider, SMTP transport, or real website-form submission is implemented. UI statuses name test records at the point of activity. A local interest submission represents neither actual company participation nor formal consent.

## Validation

```bash
uv run pytest -q
node --check src/outreach_agent/dashboard/app.js
git diff --check
```

The test suite covers the API/data behavior and checks that all seven views, hash navigation, multi-page selection, token-based test form controls, test/live labels, and same-origin-only mutation paths are present. The page layout is responsive and avoids a wide company table. Browser review should be performed at 1440×900, 1920×1080, and 2560×1440 when those viewport controls are available.
