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
| GEO Engine property registration | VERIFIED LOCALLY / ISOLATED ONLY | Mock bridge and prior isolated owner-controlled fixture tests | No live property mutation was performed in this run |
| Website audit | IMPLEMENTED IN GEO ENGINE / NOT RUN | GEO Engine audit routes and services exist | Consent-gated execution was not authorized |
| Query universe and baseline | IMPLEMENTED COMPONENTS / NOT RUN | GEO Engine research services exist | No research job or paid/model call was started |
| Strategy evaluation and experiments | IMPLEMENTED COMPONENTS / NOT RUN | GEO Engine experiment/citation services exist | Requires separate operator authorization and budget/provider approval |
| Report generation and delivery | IMPLEMENTED / TESTED LOCALLY (PREVIEW ONLY) | `src/outreach_agent/geo_result_delivery.py`, existing DOCX renderer, `research_reports` table | No email delivery is enabled or claimed |
| Background polling | IMPLEMENTED / MANUAL OR SCHEDULER-READY | Single-run Sheets sync and watcher configuration exist | Managed cloud scheduler deployment remains required |
| Deployment | VERIFIED STAGING | Render deployment `dep-dbdegpvavr4c739ppic0`, live commit `d3c1da1b84671d22dfb2cb735566d84e065aa5bf`, `/health` returned HTTP 200 | GEO Engine commit is pushed but its corresponding staging deployment/API verification remains outstanding |

## Live owner-controlled evidence

The existing two invitation references are associated with GeoAIResume (`geoairesume-owner-test-v1`, `https://geo-ai-resume.vercel.app`). They must not be relinked to Whisker Haven.

The prior live Sheets synchronization observed three records:

| Invitation | Submission | Form result | Review state |
|---|---|---|---|
| `GEO-6BF37FA993AF1A4D` | `submission-5a2262094d52a6617a1d` | YES / YES; `GeoAIResume Test Representative`; `Owner-Controlled Test Coordinator`; `yuxuan.chen.1031@outlook.com`; 2026-10-22 16:40:40 | `NEEDS_REVIEW` |
| `GEO-6BF37FA993AF1A4D` | `submission-b76063d347eedc75f449` | YES / YES; `Mel C`; `CEO`; `yuxuan.chen.1031@gmail.com`; 2026-10-22 23:45:53 | `CONFLICT_QUARANTINED` |
| `GEO-E3F846249F9D5B44` | `submission-7bbe3d76f3c1cd968bd5` | YES / NO; `yuxaun.chen.1031@outlook.com`; 2026-10-22 23:47:24 | `INTERESTED_NOT_AUTHORIZED` |

The public GeoAIResume homepage was inspected read-only. It exposed website/organization metadata and descriptive content, but no public email address. The live registry record had `contact_verification_status=NOT_VERIFIED` and an empty contact-channel list. Therefore no post-form confirmation draft can be addressed safely yet.

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
- The focused report-delivery tests verify DOCX preview persistence,
  preview-only email generation, and duplicate-import idempotency.

## Staging deployment state

The Outreach changes are deployed to the existing Render staging service:

- Service: `geo-research-outreach-staging`
- URL: `https://geo-research-outreach-staging.onrender.com`
- Deployment: `dep-dbdegpvavr4c739ppic0`
- Live commit: `d3c1da1b84671d22dfb2cb735566d84e065aa5bf`
- Render result: `Deploy succeeded | Live`
- Health check: `GET /health` returned HTTP 200 with
  `{"status":"healthy","service":"outreach-dashboard"}`.
- Unauthenticated protected-route check: `GET
  /api/recruitment/post-form-confirmation` returned HTTP 401.

No database records were changed by this implementation pass. The additive
registry initialization is backward-compatible, but a direct authenticated
read of the staging schema was not performed because no dashboard token was
exposed or retrieved. The GEO Engine integration commit `7932945` is pushed
to `codex/render-internal-database-config`, but no corresponding live GEO
Engine deployment or authenticated cross-service request is claimed.

Therefore the current evidence is: Outreach staging is live and protected;
the complete cross-repository execution is verified in isolated local tests,
not yet as a live two-service staging run.
