# GEO Research Outreach Agent — Integration Handoff

**Status:** audit and contract proposal only. No integration code, database, campaign, export, or worker state was changed for this handoff.

## Executive summary

This repository has evidence-backed company and hotel candidate stores, explicit cohort and review states, a local dashboard/API, test-only campaign simulation, an allowlisted Gmail test-send path, and stable JSONL exports for a researcher-controlled GEO Engine handoff. It does **not** currently implement a direct, bidirectional GEO Engine integration or the requested integrated audit → recommendation → experiment → report lifecycle.

The Phase 7 data must be sent as separate `SMB`/business-company and `HOTEL_PROPERTY` profile types. A hotel property may be classified `BUSINESS` and `HOTEL`; property identity must not be confused with a hotel chain/company identity. Hotels in the hotel research cohort are excluded from the 5,000-SMB count by default. An owner-controlled test site belongs to neither formal cohort.

GeoAIResume should be registered in a separate test registry with `record_classification=OWNER_CONTROLLED_TEST`, explicit owner authorization, and no formal cohort memberships. Its detected email/form channels are test evidence only. Contact-form discovery may inspect visible controls; this system must never submit a form automatically. No normal company outreach should be sent automatically either.

## Current state observed

### Data stores and schemas

| Store | Path | Shape and purpose |
|---|---|---|
| Phase 7 business pool | `data/phase7_candidates.db` | SQLite `candidates` keyed by `candidate_id`; unique `canonical_domain`; name, website, sector/industry, location, source/license/provenance, discovery timestamp, verification/eligibility/opportunity/contact/review/outreach states, evidence JSON, last inspection and source batch. `candidate_provenance` preserves multiple source records; `import_runs` records source-file hashes and import counts. `phase7_selection` and `phase7_runs` preserve deterministic selection and batch/request/runtime metrics. |
| Separate hotel property pool | `data/hotel_candidates.db` | SQLite `candidates` keyed by property-level `candidate_id`; includes website/domain, source and license, verification, GEO eligibility/opportunity, contact/review/outreach statuses, property type/address, chain name/status, amenities, room/guest evidence, operational evidence, crawl evidence and timestamps. `hotel_contact_channels` stores property-scoped email or form evidence and review state. `hotel_provenance`, `import_runs`, and `hotel_runs` preserve source and run history. |
| Original professor dashboard cohort | `data/phase6a_experimental.db` | SQLAlchemy model family in `src/outreach_agent/models.py`; `Company` uses integer ID, normalized unique domain, name/website, industry/location, source-backed size fields, eligibility/opportunity/priority and qualification evidence, independent review fields, contact status and lifecycle state. Related tables include `DiscoveryRecord`, `WebsiteSnapshot`, `WebsitePage`, `Contact`, `ContactEvidence`, and `ContactExtractionRun`. |
| Campaign history | `data/phase5d_campaigns.db` (local sidecar) | Separate SQLite store. `campaigns`, `campaign_items`, `demo_interest_submissions`, `campaign_audit`, and `demo_control`; constraints enforce `demo_only=1`, local simulated state, and simulated signup semantics. It is isolated from company qualification/review data. |
| Test Gmail send history/settings | `config/test_email.yaml`; local event store via `EmailEventStore` | Test sender/recipient configuration is separate from company contact records. OAuth client file is expected under `~/.config/geo-research-outreach-agent/gmail-oauth-client.json`; tokens are stored in OS keyring. The test endpoint validates its explicit recipient allowlist, message-preview hash and confirmation. It must not be repurposed as company campaign sending. |

The schema definitions are in `src/outreach_agent/candidate_pool.py`, `src/outreach_agent/hotels.py`, `src/outreach_agent/models.py`, `src/outreach_agent/campaigns.py`, and `src/outreach_agent/gmail_integration.py`. `data/phase7_candidates.db` and `data/hotel_candidates.db` are local operational datasets; do not copy them into a source-control integration payload.

### Counts and classifications

The project context reports 57,664 business-pool candidates and 7,172 strict-contactable all-industry companies. Those are not synonymous with verified SMBs or approved outreach recipients. The saved SMB reconciliation at `docs/phase7_smb_target_reconciliation.md` reports a prior snapshot of 370 source-evidence-supported SMBs, 353 GEO-eligible SMBs and 141 strictly contactable GEO-eligible SMBs; the 7,172 broader contactable count includes records that are not established SMBs. Use the current reconciliation export/database at integration time, not those historical counts as live values.

`src/outreach_agent/phase7_smb.py` classifies size from saved, source-reported employee count and the project's existing category. It does not infer size. `SIZE_UNKNOWN` and conflicting evidence remain review states, not automatic exclusion. Hotel properties are identified from OSM category tags and exported as `BUSINESS;HOTEL`, with `hotel_cohort_member=YES` and `smb_cohort_member=NO_BY_DEFAULT`. Shared domains are only candidate links: a chain domain does not prove two property records are the same company or same hotel.

Required count semantics for the external integration:

- `SMB_COHORT`: count a company only under the approved SMB policy; exclude hotel-cohort members by default.
- `HOTEL_COHORT`: count property identities, not chain domains; do not infer SMB eligibility from hotel classification.
- `OWNER_CONTROLLED_TEST`: never count toward either formal target, even if it resembles a business or appears in a candidate export.
- Keep discovery, identity verification, operation evidence, GEO eligibility, contact discovery, human review, outreach readiness and consent separate fields.

### Contact evidence and channel semantics

Company contacts in `models.py` preserve contact type, email, first-party source URL, extraction/evidence text, channel type, purpose, suitability/ranking, validation confidence, and separate human review. `ContactEvidence` retains source URL and extraction method. Candidate-pool hotel/business records use their associated contact channel tables/exports and preserve source URL/evidence and `review_status`.

Channel classes include public business email variants (`GENERAL_BUSINESS_EMAIL`, `BUSINESS_DEVELOPMENT_EMAIL`, `SALES_MARKETING_EMAIL`, `PARTNERSHIP_EMAIL`, and in some workflows `NAMED_PERSON_EMAIL`), `CONTACT_FORM`, support/restricted/other email categories, and contact pages without a proven submission method. A plain contact page is **not** a confirmed form. `src/outreach_agent/contact_channel_model.py` provides company-level channel coverage labels `EMAIL_ONLY`, `FORM_ONLY`, `EMAIL_AND_FORM`, and `NO_CONTACT_CHANNEL` (with unresolved evidence represented separately as `NEEDS_REVIEW`). Phase 7 uses stricter contactability: suitable first-party business email or a form with observed submission controls, attached to a verified and GEO-eligible identity. A discovered channel is not human-approved or outreach-authorized.

Form-only records remain valid contact-channel evidence. Keep them in exports and review queues; do not drop them because there is no email. Do not treat a “contact page” as a usable form unless controls were actually observed. For form-only records, the dashboard maintains manual review and does not provide automatic submission.

### Campaign, review, sending and response

- `src/outreach_agent/campaigns.py` is explicitly a local-only campaign/demo sidecar; its module documentation states there is no delivery provider. Demo campaign approval and delivery are explicitly labeled demo/simulated. Demo interest submissions are marked simulated, pending research-team review, and not formal consent.
- `src/outreach_agent/dashboard.py` exposes local API routes for companies, campaigns, response browsing/export, contact review, draft previews, and test-email operations. The server binds to loopback. Current API namespaces include `/api/companies`, `/api/candidates`, `/api/campaigns`, `/api/responses`, `/api/draft/...`, `/api/phase6g/review/...`, and `/api/test-email/...`.
- Real Gmail delivery exists only for the two exact test recipients configured in `config/test_email.yaml`, through `/api/test-email/send`; the route requires allowlist validation, an explicit `confirm`, a current preview hash, provider readiness, and idempotency reservation. Gmail API acceptance is recorded separately from confirmed inbox delivery. It is not a bulk company outreach path.
- Campaign company selections require human qualification/contact review and other eligibility gates; form items go to a manual form-review queue. The `campaigns` sidecar does not change source-company approval fields. No company email delivery or website form submission is automatically performed.
- The correct external integration must not reuse Gmail test-send code, campaign approval, or campaign records to send to GEO Engine or GeoAIResume. These systems are for read-only audit/evaluation handoff, not outreach.

### Dashboard, exports and report generation

- Dashboard entry point: `scripts/phase5b_dashboard.py`; server and endpoints: `src/outreach_agent/dashboard.py`; read model and CSV export: `src/outreach_agent/dashboard_data.py`; UI: `src/outreach_agent/dashboard/{index.html,app.js,style.css}`.
- Existing operator exports include Phase 7 outputs such as `data/phase7_contactable_companies.csv`, `data/phase7_contact_channel_audit.csv`, `data/phase7_validation_failures.csv`, `data/phase7_exclusion_queue.csv`, and `data/phase7_yield_metrics.csv`; cohort reconciliation exports include `data/phase7_smb_reconciliation.csv`, `data/phase7_smb_contactable_companies.csv`, `data/phase7_smb_size_review_queue.csv`, and `data/phase7_hotel_cohort_membership.csv`.
- A DOCX report template exists at `templates/company_geo_analysis_report_template.docx`, generated by `scripts/build_company_geo_report_template.py`. It is a template, not an automated pipeline that produces evidence-backed final reports from GEO Engine results.
- Current dashboard routes serve locally persisted read-only data plus separate local campaign state. There is no route for GEO Engine profile ingestion, callback/webhook processing, experiment result persistence, or report artifact association.

### Existing GEO Engine profile export contract

The existing one-way JSONL export is implemented by `export_geo_platform_profiles()` and `export_startup_geo_profiles()` in `src/outreach_agent/hotels.py`, invoked by:

```sh
PYTHONPATH=src uv run python scripts/phase7_hotels.py profiles
```

Paths:

- `data/geo_engine_hotel_profiles.jsonl`
- `data/geo_engine_startup_profiles.jsonl`

Each line uses `schema_version: "geo-company-profile/v1"` and stable `profile_id`.

Hotel profile fields: `profile_type: hotel_property`; `classifications`; `cohort_membership`; `identity` (name, website, canonical domain, verification state/evidence); `hotel` (category, address/location, chain signal, amenities, room evidence, guest segments, operating evidence status); `qualification` (eligibility/opportunity/evidence); `website_evidence` (URL, title, description, HTTP status, content hash); `provenance` (source name/URL/record ID/license and discovery/inspection times); `contact_channels` (type, URL, evidence URL, review status; no email address); and `review` (human state, outreach-ready state, changes-applied false).

The second file is a legacy YC-attributed `startup_company` subset, not the SMB target. It includes identity, YC source attribution/industry/location/unknown size, qualification/evidence, website metadata/hash, provenance, and review. Neither JSONL is a direct API call, a registered GEO Engine property, or proof that an audit or experiment ran. Serialization is stable/sorted and written through a temporary file then replaced.

Known evidence gap: the hotel export contains page metadata and hashes, not exact full-text excerpts/offsets. The `WebsitePage` ORM can store visible text for the older cohort, but the Phase 7 hotel/business candidate profile evidence is narrower. Do not manufacture quoted edit recommendations from titles/descriptions/hash alone.

## GEO Engine capability boundary

The sibling repository is available at `../geo-engine`. It is a separate FastAPI/React/PostgreSQL application (routes in `../geo-engine/backend/app/api/v1/`). Relevant implemented endpoints include:

- `POST /api/v1/audit/run` with `{ "property_id": <integer> }`; reads an already-registered platform property and returns its serialized audit. `GET /api/v1/audit/latest?property_id=...`, `/history`, and `/{audit_id}` read saved audits.
- `POST /api/v1/experiment-lab/run` with a `property_id` (optional in request), experiment name/description, provider/model, dataset, queries/documents, strategies, query count, seed, temperature and evaluation metrics; it schedules a background experiment and returns a serialized experiment. The route is in `../geo-engine/backend/app/api/v1/experiment_lab_routes.py`.
- GEO Engine has property-scoped audit and experiment storage and report/artifact handling. The official replication UI/API has its own run model and artifact routes. These records do not currently carry this repository's candidate ID/profile ID as a defined cross-system foreign key, and there is no handoff-agent callback endpoint.

The previously documented `/api/v1/experiment-lab/official-replications` routes execute the platform's official Princeton replication workflow; they are not a substitute for a company-specific baseline/post optimization experiment. The current company integration needs the platform's **existing property audit and normal experiment routes**, not the unrelated replication runner.

Audit work in this handoff is limited to findings and recommendations. Applying a website change requires an explicit human-approved change plan and owner-controlled execution outside the outreach agent. Recommendation generation and experiment evidence must be linked but never represented as proof that the recommendation caused a measured lift unless experimental design supports that claim.

## Proposed bidirectional contract (not implemented)

Use a versioned, idempotent adapter/API boundary. Keep company source-of-truth and crawl state in this repository; GEO Engine owns its audit/experiment results. The adapter should not share SQLite files or write directly into the other application's database.

### Outbound profile envelope

Suggested transport: HTTPS `POST /api/v1/integrations/outreach-agent/profiles` (new platform endpoint; do not assume this path exists). Authentication should be a dedicated scoped service credential. Require `Idempotency-Key` and support batch response per `profile_id`.

```json
{
  "contract_version": "outreach-geo-handoff/v1",
  "handoff_id": "stable-uuid-or-content-hash",
  "sent_at": "RFC3339",
  "profile": {
    "profile_id": "stable-source-record-id",
    "profile_type": "business_company | hotel_property | owner_controlled_test",
    "classifications": ["BUSINESS"],
    "cohort_memberships": ["SMB_COHORT"],
    "identity": {
      "name": "...", "website_url": "https://...", "canonical_domain": "...",
      "identity_status": "VERIFIED", "identity_evidence": [{"url":"...","type":"first_party_match","observed_at":"RFC3339"}]
    },
    "qualification": {
      "operational_status": "SUPPORTED | UNKNOWN | CONTRADICTED",
      "geo_eligibility": "ELIGIBLE | NEEDS_REVIEW | INELIGIBLE | UNKNOWN",
      "geo_opportunity": "...", "research_priority": "...",
      "size": {"status":"SMB_SUPPORTED | LARGE_ENTERPRISE_SUPPORTED | SIZE_UNKNOWN | CONFLICTING_EVIDENCE","value":null,"unit":"employees","source_url":null},
      "evidence": []
    },
    "source_provenance": [{"source_name":"...","source_url":"...","record_id":"...","license_or_permission":"...","observed_at":"RFC3339"}],
    "website_evidence": [{"url":"...","http_status":200,"title":"...","description":"...","content_hash":"sha256:...","captured_at":"RFC3339","excerpt":{"text":"...","start_offset":0,"end_offset":0}}],
    "contact_channels": [{"kind":"PUBLIC_BUSINESS_EMAIL | OFFICIAL_CONTACT_FORM | CONTACT_PAGE_ONLY","value":null,"url":"...","evidence_url":"...","first_party":true,"review_status":"PENDING"}],
    "review_state": {"identity":"...","company":"...","contact":"...","outreach_approval":"NOT_APPROVED"}
  }
}
```

Do not send contact PII to GEO Engine unless necessary and specifically approved; contact addresses are not required for website audit or LLM visibility experiments. For a business profile, require a verified identity and eligible website before invoking an audit. Hotel property profiles include separate property identity/address/chain evidence and may not be silently collapsed by canonical domain. Include licensed-source attribution in both records and platform exports.

### GEO Engine result envelope back to the agent

Suggested callback or polling resource (new adapter endpoint/record): `GET/POST /api/v1/integrations/outreach-agent/jobs/{handoff_id}`. Persist each response with `profile_id`, GEO Engine `property_id`, audit ID, experiment IDs, schema version and timestamps. Stable IDs and idempotency keys must permit retries without duplicate audits/experiments.

```json
{
  "contract_version": "outreach-geo-result/v1",
  "handoff_id": "...", "profile_id": "...", "property_id": 123,
  "audit": {
    "audit_id": 456, "status": "COMPLETED", "started_at":"...", "completed_at":"...",
    "findings": [{"finding_id":"...","category":"...","severity":"...","summary":"...","evidence_url":"...","evidence_excerpt":"..."}],
    "top_strategies": [{"rank":1,"strategy_id":"...","title":"...","rationale":"...","finding_ids":["..."]}],
    "text_edit_recommendations": [{"recommendation_id":"...","url":"...","selector_or_section":"...","exact_current_text":"...","proposed_replacement_text":"...","rationale":"...","evidence_ids":["..."],"approval_status":"PROPOSED"}],
    "generated_questions": [{"question_id":"...","question":"...","intent":"...","source":"..."}]
  },
  "baseline": {"experiment_id":"...","status":"...","provider":"...","model":"...","prompt_version":"...","questions":[{"question_id":"...","result":"...","citations":[]}],"metrics":{}},
  "experiment": {"experiment_id":"...","status":"QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED","strategy_ids":[],"baseline_run_id":"...","started_at":"...","completed_at":"..."},
  "post_optimization": {"run_id":"...","status":"...","provider":"...","model":"...","prompt_version":"...","questions":[{"question_id":"...","result":"...","citations":[]}],"metrics":{},"comparison_to_baseline":{}},
  "report": {"status":"READY | PENDING | FAILED","artifact_url":"...","media_type":"application/pdf | application/docx | application/json","sha256":"...","generated_at":"..."}
}
```

Contract requirements:

1. Audit result should include **findings**, no more than three ranked strategies, and exact text edits only where a retained source excerpt/selector supports them. Suggested edits remain proposals; the handoff agent cannot publish or edit the site.
2. Generated questions must be versioned and reused for baseline and post runs. Preserve provider, model, prompt/query, parameters, data timestamp and per-question result/citations so comparisons are reproducible.
3. `baseline` is a distinct run saved before any site changes. `experiment.status` is a lifecycle state, not an outcome. Post-optimization results must reference the baseline and actual approved change-set ID (or explicitly say no changes were applied).
4. Report-generation data must include source profile version, findings/strategy/edit IDs, question set, baseline/post run IDs, metrics, limitations, citations and report artifact hash. A DOCX template can render this record but does not substitute for its evidence.
5. Responses must be append-only/versioned. Failed, blocked, stale and partial runs remain visible and must not overwrite a successful historical result. Make statuses and null/missing data explicit; do not impute a result.
6. Accept only evidence URLs matching the submitted company/property identity scope. Keep hotel property IDs and owner-test IDs in their own namespaces. Record raw audit/experiment IDs and endpoint versions for provenance.

## GeoAIResume test-site registration

The adjacent `../geoairesume-web` project exists; its current site URL is configured in `src/config/site.ts` as `https://geoairesume-web-six.vercel.app`. This audit found no contact-submission API in the inspected frontend search results. Treat that as a static site/owner-controlled fixture until the owner confirms the deployed canonical URL, control of the domain, permitted crawl scope, and which page contains the test contact channel. Do not infer that a public contact form exists.

Register a separate fixture manifest (proposed; not present yet), e.g. `data/owner_controlled_test_sites.jsonl`, with:

```json
{
  "schema_version":"outreach-test-site/v1",
  "test_site_id":"geoairesume-owner-test-v1",
  "record_classification":"OWNER_CONTROLLED_TEST",
  "formal_cohort_memberships":[],
  "site_name":"GeoAIResume controlled test website",
  "canonical_url":"https://geoairesume-web-six.vercel.app",
  "owner_authorization":{"status":"PENDING_EXPLICIT_CONFIRMATION","owner":"...","scope":"read-only contact-channel discovery","confirmed_at":null,"evidence_ref":null},
  "allowed_actions":["FETCH_PUBLIC_PAGES","DETECT_EMAIL_TEXT","DETECT_FORM_CONTROLS"],
  "prohibited_actions":["SUBMIT_FORM","SEND_EMAIL","CHANGE_WEBSITE","COUNT_TOWARD_SMB_TARGET","COUNT_TOWARD_HOTEL_TARGET"],
  "contact_evidence_status":"PENDING"
}
```

Enforcement requirements for the eventual implementation:

- Test-site records reside in a fixture registry/table, not `phase7_candidates.db` or `hotel_candidates.db`; if a shared profile API requires a record, mark it `OWNER_CONTROLLED_TEST` and set both cohort memberships false with exclusion reason `CONTROLLED_TEST_FIXTURE`.
- A hard classification guard excludes that type from SMB/hotel totals, campaign selection, standard company exports, and outreach eligibility. Dashboard test-fixture metrics are separately labeled.
- Require explicit owner authorization before fetching. Scope hostnames and page paths; respect robots/access rules and rate limits just like any website. Do not use the site to test contact submissions automatically.
- Preserve form-only findings as `OFFICIAL_CONTACT_FORM` only after detecting actual form controls/action evidence; distinguish from `CONTACT_PAGE_ONLY`. Keep test evidence and source URL, captured timestamp and review status.
- Form submission must be a manual owner-operated test, if the owner chooses. The agent never POSTs, clicks submit, uploads, or follows a submit action. Likewise no outbound email is triggered by registering or auditing this fixture.
- GEO Engine jobs on this fixture are labeled `OWNER_CONTROLLED_TEST`; results cannot be aggregated into formal company-study results without explicit research protocol approval.

## Current hotel worker snapshot (read-only)

Command executed: `PYTHONPATH=src uv run python scripts/phase7_hotels.py status`. The command reported SQLite integrity `ok`, LaunchAgent loaded, `worker_running=true`, PID **12710**, checkpoint status `running`. No worker control command was invoked.

At the status read (2026-10-22): 15,262 hotel property candidates; 1,306 processed/inspected; 389 identity-verified; 372 GEO-eligible; 198 with any contact evidence (116 with business email, 108 with confirmed form, 26 with both); 7,176 pending verification. The live checkpoint showed 3,707 HTTP attempts and 4,940.9 elapsed seconds. The separate worker, checkpoint and database were not changed. Counts are a point-in-time status result; query `status` again before any future integration operation.

## Tests and audit coverage

Relevant current deterministic suites include `tests/test_hotels.py`, `tests/test_hotel_website_discovery.py`, `tests/test_phase7.py`, `tests/test_phase7_smb.py`, `tests/test_contacts.py`, `tests/test_contact_channel_model.py`, `tests/test_phase5d_campaigns.py`, `tests/test_phase5b_dashboard.py`, and `tests/test_gmail_integration.py`. They cover data import/verification/export, cohort semantics, contact channel classification, isolated demo campaign behavior, and test-email safeguards. The existing profile tests validate JSONL contract/stability rather than an API exchange with GEO Engine.

Integration gaps not covered by existing tests: cross-repository authentication/idempotency; source-to-platform property mapping; retry/reconciliation for partial batch import; owner-test fixture exclusion across every count/export; persisted platform result ingestion; exact source excerpt provenance; end-to-end baseline/strategy/post comparison; and DOCX report generation from result payloads.

## Missing functionality and recommended handoff order

1. **Confirm platform API contract and permissions.** Map the existing handoff profile to GEO Engine's property schema and choose scoped authentication. Existing audit routes need a registered `property_id`; this repository has only local candidate IDs. Add an idempotent import/lookup endpoint or a researcher-mediated mapping step before calling audits.
2. **Make content evidence adequate for exact edits.** Store permitted first-party page excerpts with URL, capture time, content hash and offsets. Do not use contact data as content evidence; minimize transfer of any personal contact data.
3. **Add explicit owner-controlled test-fixture registry and exclusion tests.** Confirm GeoAIResume ownership/authorization and exact domain first. Test fixture must stay out of both cohorts and all live send selections.
4. **Run a read-only audit and capture result envelope.** Import verified eligible profile, call existing `POST /api/v1/audit/run` with mapped platform property ID, and persist audit response linked to source `profile_id`. No website edits.
5. **Run baseline and experiment through GEO Engine.** Select generated questions, provider/model and strategies, save baseline first, create a documented experiment, require owner/researcher approval for actual site changes, then record post run. Keep baseline immutable.
6. **Render and review report.** Populate the DOCX template with source citations, top-three strategies, exact-edit proposals, generated questions, baseline/post outcomes, experiment state, limitations, and reviewer signoff. Until this exists, the template is manual only.
7. **Keep research operations independent.** Do not invoke campaigns, Gmail, contact submissions, or the hotel worker as part of GEO Engine handoff. Preserve all historical campaign records, crawler checkpoints, contact evidence and reviews.

No tests, crawler operations, campaign actions, contact submissions, email sends, commits or pushes were performed for this audit. The only execution against the hotel pipeline was its read-only `status` command.
