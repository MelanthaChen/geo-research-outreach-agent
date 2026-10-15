# Phase 5E: Multi-Page Research Outreach Dashboard

The local professor dashboard now has persistent navigation for Overview, Companies, Campaigns, Interest Form, Responses, Analytics, and Settings. The views are routed with URL hashes and rendered in a shared application shell; they reuse the Phase 5D local API, 537-company read-only Phase 6A snapshot, saved website/contact evidence, and isolated campaign SQLite sidecar.

## Start locally

```bash
uv run python scripts/phase5b_dashboard.py --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. Navigation can also be linked directly with `/#overview`, `/#companies`, `/#campaigns`, `/#interest-form`, `/#responses`, `/#analytics`, and `/#settings`.

## Page guide

- **Overview** summarizes saved cohort metrics and recent campaigns.
- **Companies** provides existing search/filter/pagination, evidence details, multi-selection across pages, and the existing invitation draft preview. “Select all matching filters” requests only IDs from `/api/company-ids`, not full company details, and keeps selection in browser memory while navigating.
- **Campaigns** creates campaigns from selected IDs, or—when none are selected—from all companies matching the active filters. Draft review remains primary; bounded test processing and test outcomes are grouped under a secondary validation section. Contact-form review and campaign history remain available.
- **Interest Form** describes the unified form and allows a local test submission using an unexpired token from a test-delivered campaign recipient. It records a local form-open event, then submits only to `/api/demo/interest`; submission is not consent.
- **Responses** uses database-backed status, campaign, source, date, environment, search, sorting, and pagination filters. It offers filtered CSV export and a selectable company-history detail view without loading every campaign's item history into the browser. Only saved test recipient-outcome and Participation Interest Form sources are available; email replies/manual entries are disabled, and live filtering is disabled because no live records exist. A sent invitation with no recorded response is distinct from an explicit decline. Interest-form submission remains distinct from formal consent.
- **Analytics** separates saved qualification breakdowns from test delivery, response, and submission counts. Live activity is shown separately as unavailable.
- **Settings** shows configuration values, unset values, and the local test transport state.

## Data and safety boundaries

Company qualification, identity evidence, contacts, and human-review decisions are loaded from the existing cohort and are not edited. Campaigns, invitation drafts, test delivery events, test tokens, test responses, submissions, and audit history remain in `data/phase5d_campaigns.db`, separate from both the experimental company database and the frozen 77-company database. Settings are informational; the dashboard does not accept credentials or enable a live provider.

The server binds to loopback. Frontend requests are same-origin only. No public participation form route, external company website request, mail provider, SMTP transport, or real website-form submission is implemented. UI records carry one compact Test indicator at the point of activity. A local interest submission represents neither actual company participation nor formal consent. Existing campaign names with legacy labels are normalized only for display; stored campaign names, submissions, and tokens are unchanged. To explicitly update a stored name, use `uv run python scripts/phase5d_campaign_demo.py rename --campaign-id <campaign-id> --name "Research Outreach Campaign"`; the command records an audit event and preserves the campaign's items and form records.

The default company database is now `data/phase6a_experimental.db`, copied from the preserved Phase 4F cohort; the Phase 4F source file is not the dashboard write target. Visible company lists are capped at 100 rows per request. Cross-page “select all matching” retrieves IDs only. Startup still constructs the in-memory company/detail read model for the full cohort; replacing it with a SQL-backed lazy detail repository remains required before asserting 5,000-company performance readiness. See [`phase6a_company_expansion.md`](phase6a_company_expansion.md).

## Validation

```bash
uv run pytest -q
node --check src/outreach_agent/dashboard/app.js
git diff --check
```

The test suite covers the API/data behavior and checks that all seven views, hash navigation, multi-page selection, token-based test form controls, test/live labels, and same-origin-only mutation paths are present. The page layout is responsive and avoids a wide company table. Browser review should be performed at 1440×900, 1920×1080, and 2560×1440 when those viewport controls are available.

Response filtering is implemented in the campaign sidecar read path (`GET /api/responses`); pagination is capped at 100 rows, while CSV export applies the same filters and stable ordering to all matches. Contact email is used only for server-side search and is intentionally excluded from the export. The response detail endpoint includes campaign/company context and saved channel evidence, but omits form personal data. Company, frozen validation, and campaign databases are not modified by response browsing/export.
