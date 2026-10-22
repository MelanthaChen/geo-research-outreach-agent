# Phase 1 integration implementation

Implemented locally on 2026-10-22. The integration is an adapter and evidence
store; it does not implement GEO Engine audit, experiment, evaluation,
deployment, campaign, email, or form-submission logic.

## Contract and compatibility

The sibling repository's `../geo-engine/schemas/geo-research-integration/v1`
files are available and were used as the authoritative Phase 1 contract. The
existing `geo-company-profile/v1` serializers in `src/outreach_agent/hotels.py`
were not changed. `src/outreach_agent/integration.py` adds:

- `build_profile_import()` for a versioned `profile_import` record;
- a separate `geo-research-integration/v1` result adapter;
- property mapping, audit, ranked-strategy, exact-edit, question-set,
  baseline, deployment, post-deployment, and report-result records;
- explicit preservation of `PENDING`, `FAILED`, `BLOCKED`, `PARTIAL`, and
  other caller-supplied states;
- append-only JSONL result persistence with content-based retry idempotency and
  conflicting-retry detection.

The adapter retains the original source profile snapshot and source profile ID
on every imported result record. It does not call GEO Engine endpoints or
duplicate its audit/experiment/evaluation logic.

## Owner-controlled test registry

`data/owner_controlled_test_sites.jsonl` registers
`https://geo-ai-resume.vercel.app` as `OWNER_CONTROLLED_TEST`, with no formal
cohort memberships. The registry prohibits campaign eligibility, normal
company outreach, automatic email, and automatic form submission. It is a
fixture registry and is not inserted into `data/phase7_candidates.db` or
`data/hotel_candidates.db`.

## Contact discovery

`discover_public_contact()` accepts already supplied public HTML and performs
no network request or submission. It preserves source URL, evidence text,
capture time, channel type, and review status. A form is classified only when
an actual form plus a control or action is present. A contact page with no
observed controls remains `NO_CONTACT_CHANNEL`; it is not inferred to be a
working form. The classifications are `EMAIL_ONLY`, `FORM_ONLY`,
`EMAIL_AND_FORM`, and `NO_CONTACT_CHANNEL`.

## DOCX reports

`src/outreach_agent/reporting.py` reuses
`templates/company_geo_analysis_report_template.docx` and appends a clearly
labelled Phase 1 result section. It renders supplied values only and labels
missing values as `Not supplied`. The report separates observed evidence,
proposals, simulated experiments, real baseline measurements, verified
deployments, post-deployment measurements, and limitations. It cannot create
quotes, LLM responses, citations, rankings, or improvements that are absent
from the result envelope.

## Verification

Focused tests are in `tests/test_integration_phase1.py` and
`tests/test_reporting.py`. They cover owner-test exclusions, property identity
preservation, contact classification, import idempotency/conflicts, missing
experiment results, and fixture-based DOCX generation. Existing profile export
tests remain unchanged and continue to exercise backward compatibility.

## Unresolved dependencies

- The GEO Engine schemas define the record shapes, but this repository still
  has no authenticated GEO Engine import/callback endpoint or source-to-platform
  property mapping service. This implementation therefore stops at local
  versioned envelopes and an append-only import store.
- Owner authorization for GeoAIResume is still marked
  `PENDING_EXPLICIT_CONFIRMATION`; discovery can be used only after that
  authorization is confirmed by the owner/research protocol.
- No live experiment, deployment, post-deployment measurement, or report
  artifact was available in this repository, so those fields remain explicit
  pending/missing data in fixtures and reports.
- `python-docx` is now a runtime dependency for the report generator; the
  template is reused rather than regenerated.

No hotel worker command, request/time cap, checkpoint, or SQLite database was
modified. No email, form submission, campaign, paid LLM call, commit, or push
was performed.

