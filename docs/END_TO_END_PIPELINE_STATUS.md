# End-to-End GEO Research Pipeline Status

Updated: 2026-10-23

This document records the implemented pipeline as it exists in the repository and the evidence currently available. It does not promote fixtures, local tests, or independent website analysis to live research results.

## Current acceptance summary

| Stage | Status | Evidence / source | Current blocker |
|---|---|---|---|
| Discovery and qualification | IMPLEMENTED | Lean prospect registry and promotion services; source databases remain read-only inputs | Cloud discovery is disabled until staging approval |
| Invitation/reference generation | IMPLEMENTED / TESTED LOCALLY | `src/outreach_agent/research_lifecycle.py`, Google Forms integration, invitation-reference tests | No new invitations are authorized by this run |
| Google Forms response ingestion | VERIFIED LIVE (prior run) | Live Sheets sync read three responses, preserved timestamps, invitation references, and duplicate/conflict records | No new responses were submitted for this run |
| Human consent review | IMPLEMENTED / TESTED LOCALLY | `review_questionnaire_submission()` and protected dashboard review routes | GeoAIResume has two distinct YES/YES submissions under one invitation and remains quarantined |
| Email-role separation | IMPLEMENTED / TESTED LOCALLY | `invitation_recipient_email`, `representative_email`, and registry contact channels are stored separately | Current live record has no verified official company email |
| Official-company contact selection | IMPLEMENTED / TESTED LOCALLY | `create_post_form_confirmation_draft()` accepts only one explicitly verified official business email | GeoAIResume live `contact_channels_json` is empty and status is `NOT_VERIFIED` |
| Post-form confirmation | IMPLEMENTED / TESTED LOCALLY | Additive `confirmation_emails` table, protected POST route, preview-only draft creation | The live Neon database had not received this migration at the last read-only audit |
| Confirmation policy | IMPLEMENTED / TESTED LOCALLY | `OUTREACH_CONFIRMATION_POLICY`; safe default `AFFIRMATIVE_REPLY_REQUIRED`; invalid values fail closed | Production policy still requires deployment/configuration review |
| Outlook reply evidence | IMPLEMENTED / TESTED LOCALLY | `.eml` importer preserves sender, recipient, subject, Message-ID, threading headers, timestamp, and raw content | No real `.eml` was supplied in this run; importer never grants consent automatically |
| GEO Engine property handoff | IMPLEMENTED / TESTED LOCALLY | Versioned adapter and idempotent `process_geo_handoff()` | Current GeoAIResume response cannot be handed off while conflict remains quarantined |
| GEO Engine property registration | VERIFIED LOCALLY / LIVE API READY | Mock bridge and prior isolated owner-controlled fixture tests; deployed integration health is ready | No authenticated property mutation was performed because the live Render service is not confirmed as an isolated test database |
| Website audit | IMPLEMENTED IN GEO ENGINE / NOT RUN | GEO Engine audit routes and services exist | Consent-gated execution was not authorized |
| Query universe and baseline | IMPLEMENTED COMPONENTS / NOT RUN | GEO Engine research services exist | No research job or paid/model call was started |
| Strategy evaluation and experiments | IMPLEMENTED COMPONENTS / NOT RUN | GEO Engine experiment/citation services exist | Requires separate operator authorization and budget/provider approval |
| Report generation and delivery | IMPLEMENTED / TESTED LOCALLY (PREVIEW ONLY) | `src/outreach_agent/geo_result_delivery.py`, existing DOCX renderer, `research_reports` table | No email delivery is enabled or claimed |
| Background polling | IMPLEMENTED / MANUAL OR SCHEDULER-READY | Single-run Sheets sync and watcher configuration exist | Managed cloud scheduler deployment remains required |
| Deployment | VERIFIED STAGING | Outreach deployment `dep-dbdet7favr4c739r65ag` at `a4a87dc3a47c677346059062276c7cfd198a78d2`; GEO Engine deployment `dep-dbderul9fdbs73at310g` at `2131234c520a22c221d714126397c9904b741136` | Authenticated cross-service write requires a confirmed isolated GEO database |

## Live owner-controlled evidence

The existing two invitation references are associated with GeoAIResume (`geoairesume-owner-test-v1`, `https://geo-ai-resume.vercel.app`). They must not be relinked to Whisker Haven.

The prior live Sheets synchronization observed three records:

| Invitation | Submission | Form result | Review state |
|---|---|---|---|
| `GEO-6BF37FA993AF1A4D` | `submission-5a2262094d52a6617a1d` | YES / YES; `GeoAIResume Test Representative`; `Owner-Controlled Test Coordinator`; `yuxuan.chen.1031@outlook.com`; 2026-10-22 16:40:40 | `NEEDS_REVIEW` |
| `GEO-6BF37FA993AF1A4D` | `submission-b76063d347eedc75f449` | YES / YES; `Mel C`; `CEO`; `yuxuan.chen.1031@gmail.com`; 2026-10-22 23:45:53 | `CONFLICT_QUARANTINED` |
| `GEO-E3F846249F9D5B44` | `submission-7bbe3d76f3c1cd968bd5` | YES / NO; `yuxaun.chen.1031@outlook.com`; 2026-10-22 23:47:24 | `INTERESTED_NOT_AUTHORIZED` |

The public GeoAIResume homepage was inspected read-only. It exposed website/organization metadata and descriptive content, but no public email address. The live registry record had `contact_verification_status=NOT_VERIFIED` and an empty contact-channel list. Therefore no post-form confirmation draft can be addressed safely yet.

## Deployed cross-service verification (2026-10-23)

### Current runtime follow-up

The existing Render environment uses the existing `GEO_ENGINE_HANDOFF_TOKEN`
mechanism only. A shared secret is configured in both services; values were
handled only through Render's masked environment UI and were not written to
the repository or logs. The deployed client sends canonical Bearer
authentication plus the proxy-safe `X-GEO-Engine-Handoff-Token` header, and
the GEO Engine integration boundary validates either header against the same
secret.

The live read-only smoke request to the GEO Engine result route returned HTTP
404 (`research result not found`) with the configured credential, while missing
and invalid credentials return HTTP 401. The 404 proves the request passed the
authentication dependency; there is no result fixture for that probe. No
profile import, research pipeline call, or other write request was attempted.

The GEO Engine database inspection identified the existing Render Postgres
target by safe metadata only: host prefix `dpg-dar8lb60tbcc739en720-a` and
database `geo_engine_db_o8k7`. It is attached to the existing `geo-engine`
service and was not independently confirmed as an isolated staging database.
Render's free plan does not expose a service shell for runtime fingerprinting,
and creating a separate isolated database would require an additional cloud
resource/plan decision. No cloud database mutation was performed.

Consequently, the live acceptance test remains blocked before mutation: no
Property ID, research-job ID, result-envelope ID, or report ID is claimed for
the deployed services. The clean owner-controlled fixture acceptance path,
including idempotency, remains verified by the local cross-repository tests
below. No historical consent, production GEO data, email, paid provider call,
or research job was changed or started.

The existing Render GEO Engine service was manually deployed, without creating a
second service, from the targeted authentication-fix commit:

- Service: `geo-engine` (`srv-d8g7ucu7r5hc73aq03q0`)
- URL: `https://geo-engine.onrender.com`
- Deployment: `dep-dbderul9fdbs73at310g`
- Live commit: `2131234c520a22c221d714126397c9904b741136`
- Render result: `Deploy succeeded | Live`
- Source: `MelanthaChen/geo-engine`, branch `main`

The live API checks were read-only or deliberately unauthorized:

| Request | Result | Meaning |
|---|---:|---|
| `GET /health` | HTTP 200, `{"status":"healthy"}` | Service is reachable and healthy |
| `GET /api/v1/integration/health` | HTTP 200, `{"status":"ready","scope":"property_registration_only"}` | Integration routes are deployed and advertise the restricted scope |
| `POST /api/v1/integration/profile-import` without a token | HTTP 401, `authenticated handoff token required` | Unauthenticated handoff is rejected before mutation |
| `POST /api/v1/integration/profile-import` with a deliberately invalid token | HTTP 401, `authenticated handoff token required` | Invalid credentials are rejected |
| `GET /api/v1/integration/research-results/fixture-company` without a token | HTTP 401, `authenticated result access required` | Result retrieval is protected |
| Outreach `GET /health` | HTTP 200, `{"status":"healthy","service":"outreach-dashboard"}` | Outreach service is reachable |
| Outreach protected API with a deliberately invalid token | HTTP 401 | Dashboard authentication remains enforced |

The deployment environment inspection confirmed the shared secret is configured
through the existing `GEO_ENGINE_HANDOFF_TOKEN` mechanism on both services;
values were not logged or placed in source control. The client keeps Bearer as
the canonical header and also sends `X-GEO-Engine-Handoff-Token` for managed
proxy compatibility. GEO Engine validates either header against the same
secret, with missing and invalid values still rejected.

A real read-only request to
`GET /api/v1/integration/research-results/auth-smoke-nonexistent` using the
configured value reached the authenticated route and returned HTTP 404
(`research result not found`). This proves authentication succeeded; the
requested result fixture was simply absent. Missing and invalid credentials
continue to return HTTP 401.

Even if a secret were available, `geo-engine` is the existing service rather
than a separately confirmed isolated test service/database. A profile import,
research pipeline call, or report retrieval could therefore mutate its actual
configured database. No such write was attempted. This is a safety blocker,
not an authentication failure claim.

## Confirmation workflow rules

The three email roles are distinct:

1. `invitation_recipient_email`: address used for the initial invitation.
2. `representative_email`: address entered by the form respondent, stored for accountability.
3. `official_company_email`: one verified business contact from the registry, used for a post-form confirmation draft.

The implementation never substitutes role 1 or 2 for role 3. A draft is blocked when authorization is explicitly `NO`, when a distinct response remains quarantined, when the official contact is unverified, or when more than one verified official email is available.

The explicit policy setting is:

```text
OUTREACH_CONFIRMATION_POLICY=AFFIRMATIVE_REPLY_REQUIRED
```

`AFFIRMATIVE_REPLY_REQUIRED` is the safe default. `NOTICE_ONLY` is available only through explicit configuration. Neither sending a confirmation nor the absence of a reply grants consent. Review and handoff remain separate protected operations.

## Safe next actions

1. Resolve the two YES/YES submissions for `GEO-6BF37FA993AF1A4D` through the existing human review UI; do not delete either response.
2. Add a verified official GeoAIResume business email with source URL/evidence, or leave the record blocked.
3. Apply the additive migration to the intended staging database, then verify `confirmation_emails` and the new email-role fields.
4. Deploy the current branch and configure the explicit confirmation policy.
5. Generate a preview only. Approval and sending require a separate operator decision.
6. After applicable review requirements are satisfied, run the existing idempotent property-registration handoff. Do not start audits or experiments automatically.

## Verification commands

Focused regression suite:

```bash
uv run pytest -q tests/test_research_lifecycle.py tests/test_phase6g_dashboard.py \
  tests/test_google_forms_integration.py tests/test_geo_engine_handoff.py \
  tests/test_phase5b_dashboard.py
```

Import a real Outlook export without exposing its body in logs:

```bash
uv run python scripts/import_outlook_reply.py /secure/path/reply.eml \
  --database "$OUTREACH_DATABASE_URL"
```

The importer stores the raw evidence in the registry but prints only correlation metadata. Do not place credentials, `.eml` files, or database URLs in the repository.

## Explicit non-claims

- No invitation, confirmation email, or reply was sent by this implementation pass.
- No form response was submitted by this implementation pass.
- No live consent decision was changed.
- No live GEO Engine property, audit, experiment, monitoring job, deployment, or paid evaluation was started.
- Independent website analysis artifacts, if present, are not consent-gated Outreach research results.
- Local and isolated tests are not evidence of live deployment.

## Newly implemented local integration

The following concrete additions complete the safe, controlled local path without
touching the historical GeoAIResume records:

- GEO Engine `app/integration/research_pipeline.py` composes Property import,
  existing website-audit service, deterministic question sets, persisted Query
  rows, a controlled fixture Experiment row, rule-based strategy ranking, and
  the versioned `research_result_export` envelope.
- GEO Engine `GeoResearchIntegrationAdapter` now persists query-universe and
  baseline references and exports one idempotent result envelope.
- GEO Engine exposes authenticated `GET
  /api/v1/integration/research-results/{source_profile_id}` for result retrieval.
- Outreach `HttpGeoEngineBridge` supports authenticated result retrieval.
- Outreach `research_reports` persists the result association and generated
  DOCX preview with `delivery_status=NOT_SENT`; repeat imports are idempotent.
- `report_email_preview()` produces a preview-only final email and never calls
  a provider.

## Local controlled acceptance evidence

The new cross-repository fixture test uses a clean in-memory GEO Engine
database and an isolated integration store. It verifies:

1. one stable Property is created;
2. Query rows and a controlled Experiment row are persisted;
3. the audit, strategy, question-set, baseline, and result references are
   written;
4. a repeated pipeline call returns the existing result and does not create a
   second Property, Experiment, or result envelope; and
5. Outreach creates one report preview and returns it on repeat import.

The controlled provider is explicitly labelled `CONTROLLED_FIXTURE`; no paid
model call, citation test, deployment, monitoring job, or website mutation is
performed.

Test evidence for this implementation pass:

- Outreach: `uv run pytest -q` completed successfully with 336 passing tests.
- GEO Engine integration: `PYTHONPATH=. uv run pytest -q
  tests/test_geo_research_integration.py tests/test_cross_repository_owner_fixture.py`
  completed with 10 passing tests.
- Focused authentication regression: Outreach handoff tests completed with 6
  passing tests; GEO Engine integration-auth and research-integration tests
  completed with 11 passing tests. The deployed auth behavior was additionally
  verified against the live read-only endpoint: valid credentials returned 404
  for a missing fixture, while missing and invalid credentials returned 401.
- The focused report-delivery tests verify DOCX preview persistence,
  preview-only email generation, and duplicate-import idempotency.

## Staging deployment state

The Outreach changes are deployed to the existing Render staging service:

- Service: `geo-research-outreach-staging`
- URL: `https://geo-research-outreach-staging.onrender.com`
- Deployment: `dep-dbdet7favr4c739r65ag`
- Live commit: `a4a87dc3a47c677346059062276c7cfd198a78d2`
- Render result: `Deploy succeeded | Live`
- Health check: `GET /health` returned HTTP 200 with
  `{"status":"healthy","service":"outreach-dashboard"}`.
- Unauthenticated protected-route check: `GET
  /api/recruitment/post-form-confirmation` returned HTTP 401.

No database records were changed by this implementation pass. The GEO Engine
database remains the existing Render-managed database and was not independently
confirmed as an isolated staging database, so no profile import, research job,
or report retrieval write was attempted. Render Free also does not support
one-off jobs, preventing a safe execution of the deployed Outreach client from
the service shell.

### Historical blocked audit (superseded by the completed acceptance below)

The following section records the earlier state before the isolated Neon and
Render staging resources were created. It is retained for provenance only; the
completion evidence is in “Isolated cloud acceptance completed” below.

The authenticated Render environment was inspected without revealing secret
values. The live GEO Engine service is:

- Service: `geo-engine` (`srv-d8g7ucu7r5hc73aq03q0`)
- URL: `https://geo-engine.onrender.com`
- Runtime plan shown by Render: `Free`
- Live deployment: `dep-dbderul9fdbs73at310g`
- Live commit: `2131234c520a22c221d714126397c9904b741136`

Its environment contains a single `DATABASE_URL` binding and no separate
staging database identifier, staging schema setting, or staging service. The
configured database target is the existing GEO Engine database; its connection
value was not copied or logged. No safe isolation boundary could therefore be
demonstrated. Creating a separate database/service would be a new cloud
resource and requires provider provisioning/plan approval before it can be
used.

Cloud-write acceptance result:

| Item | Result |
|---|---|
| Isolated GEO staging database | NOT AVAILABLE / NOT CONFIRMED |
| Cloud Property ID | None; no write attempted |
| Cloud research-job ID | None; no research job started |
| Cloud result-envelope ID | None; no result retrieval for a persisted fixture |
| Cloud Outreach report ID | None; no report association write attempted |
| Local controlled fixture | VERIFIED by existing cross-repository tests; idempotent Property/result/report behavior covered |
| Production/GEO database mutation | None |

The deployed authenticated read-only boundary remains verified: valid
credentials return business-level HTTP 404 for a missing result fixture, while
missing and invalid credentials return HTTP 401. This does not constitute a
cloud write or a successful deployed end-to-end mutation test.

Therefore the current evidence is: GEO Engine is live on the auth-fix commit,
its authenticated read-only boundary is verified, and Outreach is live from
the matching client commit. The
complete cross-repository mutation test remains intentionally blocked pending an
isolated GEO staging database.

## Exact remaining acceptance steps

1. Provision or identify an explicitly isolated GEO staging database/service;
   do not repoint the existing `geo-engine` service.
   staging resources, not the existing production database.
3. Seed one clean owner-controlled fixture in the isolated Outreach staging
   database with explicit verified consent; do not use the historical
   GeoAIResume conflict records.
4. Call the authenticated profile-import endpoint once, run the controlled
   fixture research pipeline, retrieve the result envelope, and persist one
   Outreach report preview.
5. Repeat the same request and verify one Property, one research result, one
   Outreach report, and no duplicate records.

Until steps 1-3 are complete, no live property or research records should be
created.

## Isolated cloud acceptance completed (2026-10-23)

The previously blocked cloud test was completed against a separate free-tier
staging stack. The production GEO Engine service and database were not
repointed or modified.

### Environment

- Neon project: `geo-engine-staging` (`rough-paper-82218218`), AWS US East 2
  (Ohio), database `neondb`.
- GEO Engine staging Render service: `geo-engine-staging`
  (`srv-dbdf2nt9fdbs73atqgp0`),
  [https://geo-engine-staging.onrender.com](https://geo-engine-staging.onrender.com).
- GEO Engine staging deployment: `dep-dbdfcf6i0phs739k6n5g`, source commit
  `7c054d74326d418bfb0e3827ec0016a9bf174c81`, status `Deploy succeeded | Live`.
- Outreach staging remained on
  `https://geo-research-outreach-staging.onrender.com`, commit
  `a4a87dc3a47c677346059062276c7cfd198a78d2`.

### Actual cross-service evidence

The fixture was `cloud-owner-fixture-20261023-v2`, an owner-controlled
GeoAIResume profile with explicit controlled-fixture consent and no formal
cohort membership.

| Stage | Actual result |
|---|---|
| Authenticated profile import | HTTP 200; `property_id=2`; `exclusion_reason=CONTROLLED_TEST_FIXTURE` |
| Duplicate profile import | HTTP 200; same `property_id=2` and no second property mapping |
| Invalid-token research request | HTTP 401 |
| Controlled research run | HTTP 200; `status=COMPLETED`; `audit_id=2`; baseline `experiment_id=2`; no paid provider |
| Repeated research run | HTTP 200; `idempotent=true`; same property, audit, experiment, and result record IDs |
| Versioned result retrieval | HTTP 200 from `/api/v1/integration/research-results/cloud-owner-fixture-20261023-v2?source_profile_version=geo-company-profile/v1` |
| Normal GEO Property API | HTTP 200 for `/api/v1/properties/2`; property domain `geo-ai-resume.vercel.app` |
| Normal Property metrics API | HTTP 200; no generated/published content and no provider executions |
| Outreach result/report preview | Isolated SQLite only: report `report-2bce086ec6d8fb352661`, DOCX created, second import returned `EXISTING` |
| Final email preview | `PREVIEW_ONLY`, recipient `owner-controlled@example.test`; no delivery attempted |

Result envelope:
`research-result:cloud-owner-fixture-20261023-v2:geo-company-profile/v1`.
The generated DOCX is at
`/tmp/geo-cloud-reports-v2/report-2bce086ec6d8fb352661.docx`; its registry
association is in `/tmp/geo-cloud-outreach-acceptance-v2.db`. These are
isolated acceptance artifacts, not production Outreach data.

### Scope and limitations

The deployed research runner is protected by the existing shared handoff
token, requires `APP_ENV=staging`/`test`/`development`, and requires an
explicit `controlled_fixture=true` owner-controlled payload. It does not
accept provider credentials, run paid LLM calls, deploy content, enable
monitoring, or start citation tests. The production GEO Engine service remains
on its prior deployment and database.

The cloud GEO persistence and retrieval path is verified. Outreach report
association and DOCX/email-preview generation were verified against an
isolated local registry because the deployed Outreach service does not expose a
public result-import mutation endpoint; no live Outreach records were changed.
Hotel discovery, outreach sending, consent changes, and production research
execution remain disabled.

## Final deployed GEO result-import verification — 2026-10-23

The authenticated GEO Engine → Outreach report-persistence boundary is now
deployed and verified with the isolated owner-controlled fixture
`cloud-owner-fixture-20261023-v2`.

| Check | Evidence |
|---|---|
| Outreach deployment | Commit `b7e3492db7eddecba76f90b0098af7a278e7fd44`; Render deployment `dep-dbdfq8gl0d7c739aplag`; service healthy at `https://geo-research-outreach-staging.onrender.com` |
| GEO deployment | Commit `7c054d74326d418bfb0e3827ec0016a9bf174c81`; Render deployment `dep-dbdfocui0phs739ldte0`; service healthy at `https://geo-engine-staging.onrender.com` |
| Authenticated result retrieval | HTTP 200; result record `research-result:cloud-owner-fixture-20261023-v2:geo-company-profile/v1`; `property_id=2`; contract `geo-research-integration/v1` |
| Result import | HTTP 200 `EXISTING`; report `report-2bce086ec6d8fb352661`; `delivery_status=NOT_SENT` |
| Idempotency | One `research_reports` row for the report ID; repeated import created no duplicate |
| Persisted association | Neon staging: fixture `CONSENTED`, `REPORT_READY`, `geo_property_id=2`, handoff `HANDED_OFF`; report row exists with `property_id=2` |
| Authorization failures | Invalid result-import credentials return HTTP 401; missing/invalid credentials remain rejected |
| Health | Outreach `/health` reports `healthy` |
| Side effects | No email sent, no paid provider call, no production or historical consent mutation |
| Regression tests | Focused integration suite: 20 passed |

The result-import endpoint is protected, validates the versioned envelope and
handoff/property association, persists through the existing report store, and
returns an idempotent existing result on retry. The test used only the
isolated Neon staging registry and the owner-controlled fixture.

Remaining production blocker: the GEO integration result envelope and generated
DOCX currently use the Render service filesystem. They must move to durable
PostgreSQL/object storage before production reliance across redeploys. The
staging database association is durable; no real outreach delivery was
attempted.

## Whisker Haven owner-controlled cloud acceptance — 2026-10-23

This section records the explicitly authorized staging test for the fictional
Whisker Haven demo site. It is separate from formal participant consent and
does not change historical GeoAIResume records.

| Stage | Actual evidence |
|---|---|
| Form submission | Live Google Form submission recorded successfully; invitation reference `GEO-FCB07DC22A54ACFB`; representative `Yuxuan Chen`, role `Owner-controlled staging test operator`, email `yuxuan.chen.1031@gmail.com`; interest `YES`; authorization `YES` |
| Google Sheets synchronization | Dry run: `read_ok rows=4 dry_run=true`; live Neon staging sync: `sync_ok seen=4 processed=0 duplicates=3 needs_review=1` |
| Outreach state | Neon staging record `whisker-haven-owner-demo-20261023-v1` remains `NEEDS_REVIEW`; no consent or GEO handoff was auto-approved |
| GEO profile import | Authenticated HTTP 200; profile `whisker-haven-independent-20261023-v2`; property ID `3`; property domain `whisker-haven-demo.vercel.app`; controlled-test exclusion preserved |
| GEO deployment | Render service `geo-engine-staging`; deployment `dep-dbdm613rjlhs73dp9ing`; commit `c22ae7f84feda01f42dcd81996d72684a29d642e`; status `Deploy succeeded | Live` |
| Controlled research workflow | Authenticated HTTP 200; `status=COMPLETED`; audit ID `5`; experiment ID `5`; baseline provider `CONTROLLED_FIXTURE`; no paid provider calls |
| Result retrieval | Authenticated HTTP 200; result `research-result:whisker-haven-independent-20261023-v2:geo-company-profile/v1`; property ID `3`; contract `geo-research-integration/v1` |
| Retry/idempotency | Repeated profile import returned property ID `3`; repeated research run returned HTTP 200 with `idempotent=true`; no duplicate property or result record |
| Invalid credentials | Read-only result retrieval without credentials returned HTTP 401 |
| DOCX preview | Generated `/tmp/Whisker-Haven-independent-GEO-report.docx`; preview labels the work as independent owner-controlled analysis and records missing rendered website evidence |

The first Whisker run exposed that the Render Free runtime has no guaranteed
Chromium executable. The deployed fix makes controlled-fixture research use
the existing bounded HTTP-first audit path rather than failing with HTTP 500.
The resulting audit is `insufficient_analyzable_content` with zero page
references because Whisker Haven is a client-rendered demo. Therefore this is
not a claim of a complete rendered-content audit or measured search visibility
improvement. A browser-capable staging runtime is still required for that
portion of the research workflow.

The GEO result is intentionally not imported into Outreach: the corresponding
live form response remains protected `NEEDS_REVIEW`, and no direct database
consent mutation was performed. No email, campaign, paid model call, or
production research job was initiated.

Local verification after this run: the Outreach full suite passed with the
development dependencies installed. GEO Engine integration regression tests
passed (`11 passed`), including the distinct owner-controlled-domain test. The
GEO Engine repository-wide suite reported `184 passed, 7 failed`; those seven
failures are in the pre-existing Brave/provider and teacher-provenance areas,
outside this integration change and its two unrelated existing working-tree
files. Those files were not modified.

## Whisker Haven evidence-backed report correction — 2026-10-23

The earlier Whisker Haven DOCX was not acceptable as a completed research
report: it contained no rendered page references, no grounded recommendations,
and a controlled-fixture baseline that was easy to misread as a real model
measurement. The report pipeline has now been corrected and rerun.

| Check | Actual result |
|---|---|
| Browser-rendered audit | 5 pages captured with Chromium/Codex browser runtime at `2026-10-23T13:57:17Z`; URLs, titles, metadata, rendered headings, internal links, source quotes, and structured-data checks are persisted in `data/whisker_haven_independent_analysis_20261023.json` |
| Recommendations | 3 concrete, page-specific proposals, each tied to an observed URL and exact quoted source text; recommendations are hypotheses, not measured lifts |
| GEO questions | 4 site-specific questions derived from the rendered homepage, cat directory, process, about, and contact content |
| Real AI visibility baseline | `INCOMPLETE`; provider `CONTROLLED_FIXTURE`, model calls `0`, no real model responses or citations; no paid provider was called and no free provider was configured |
| DOCX output | `demo_runs/whisker_haven_20261023/Whisker-Haven-GEO-Analysis.docx`; 3 rendered pages, readable tables, no template placeholders, and no raw JSON dump |
| Consent boundary | Independent owner-controlled analysis only; the live Outreach record remains `NEEDS_REVIEW`; no consent-gated handoff was claimed |
| External side effects | No emails, website changes, experiments, paid calls, production changes, or GEO handoff were initiated |

The report now explicitly labels itself `REPORT STATUS: INCOMPLETE` because a
real AI visibility baseline is absent. The cloud Render audit remains
`insufficient_analyzable_content` with no page references because its HTTP-first
runtime cannot render the client-side demo; the five-page browser audit is the
durable evidence source for this independent report. The next required action
for a completed visibility study is authorization and configuration of a free
or explicitly approved real model provider, followed by rerunning the recorded
question set. The renderer remains backward-compatible for older integration
envelopes by producing an explicit incomplete preview rather than failing or
claiming completion.
