# Cross-repository integration report

Date: 2026-10-22

Scope: first outreach-first local integration using GeoAIResume as an `OWNER_CONTROLLED_TEST` fixture. No email, form submission, paid LLM call, deployment, commit, push, database migration, crawl checkpoint, or hotel worker operation was performed.

## Result

The deterministic integration test passes. The fixture moves through the required outreach-first lifecycle as simulated/local records, then crosses the GEO Engine adapter boundary only after verified website-analysis consent. GeoAIResume remains excluded from SMB and hotel cohorts.

The test is:

```sh
cd "/Users/melc/Documents/Intern/LLM Search Optimizer/geo-engine/backend"
PYTHONPATH=. pytest -q tests/test_cross_repository_owner_fixture.py
```

Result: `1 passed`.

## Compatibility matrix

| Contract area | Outreach Agent | GEO Engine | GeoAIResume fixture | Compatibility / action |
|---|---|---|---|---|
| Source profile | `geo-company-profile/v1`; stable `profile_id`; `identity.website` | Normalizes legacy `website`/`website_url` into `geo-research-integration/v1` | `geo-research-integration/v1` fixture metadata | Compatible after the existing normalizer; no duplicate schema added. |
| Profile type | `owner_controlled_test`, `hotel_property`, business mapping | Same normalized values; owner fixture gets `CONTROLLED_TEST_FIXTURE` exclusion | `owner_controlled_test_site` | Compatible; fixture has no formal cohort membership. |
| Consent | Questionnaire submission stores explicit consent and website-analysis scope | Import requires `status=VERIFIED`, ID, version, scope, and non-expired validity | Owner approval is separate from public contact enablement | Local test explicitly maps the verified questionnaire record into the GEO import envelope. |
| Handoff | Idempotent `(profile_id, profile_version)` handoff record | Adapter accepts only normalized, consented profiles | Local-only fixture protocol | Fixed: owner fixture handoff requires `fixture_protocol=OWNER_CONTROLLED_TEST_LOCAL_ONLY`; normal profiles remain consent-gated. |
| Property mapping | Stores source identity and handoff | Reuses/creates a `Property` by normalized domain; records mapping | Canonical `geo-ai-resume.vercel.app` | Compatible and retry-safe; hotel shared-domain collision remains rejected. |
| Audit | Receives result envelope only | Owns audit execution and evidence references | No live crawl; audit runner is mocked | Compatible; audit evidence is explicitly marked local/mock. |
| Experiment reference | Imports baseline/post/deployment references without claiming results | Owns experiment records and scientific execution | No paid provider run | Compatible; baseline is `READY` with `results_present=false` in the fixture test. |
| Result envelope | Append-only JSONL import keyed by stable record IDs/content | Versioned result records and schemas | Report identity is GeoAIResume | Compatible; repeated import skips identical records and rejects conflicting retries. |
| Deployment receipt | Preserves receipt and approval/verification state | Validates and deduplicates receipt | No deployment | Compatible; receipt is `not_deployed`/rejected in the mock. |
| Monitoring | Lifecycle ends at `MONITORING_PENDING` | Internal plan is paused until verified implementation; run gate checks consent, deployment, budget, and active status | No ongoing measurement authorization is executed | Compatible; test proves pre-implementation monitoring is blocked. |
| Withdrawal/errors | Withdrawal changes research and monitoring to blocked states | Import rejects non-verified/withdrawn consent | Fixture remains owner-controlled | Fixed: handoff now checks the current record consent state, so withdrawal blocks retries. |

The Outreach and GEO Engine repositories now expose the same explicit `OUTREACH_FIRST_LIFECYCLE` tuple. GEO Engine's pre-existing internal execution lifecycle remains unchanged.

## Required lifecycle evidence

The test asserts this exact sequence:

`DISCOVERED → CONTACT_VERIFIED → PRELIMINARY_GEO_SCREENED → EMAIL_DRAFTED → INVITED (SIMULATED) → QUESTIONNAIRE_SUBMITTED (SIMULATED) → CONSENT_VERIFIED → GEO_HANDOFF → GEO_PROPERTY_RESOLVED → WEBSITE_AUDIT → QUESTION_SET_CREATED → BASELINE_READY → STRATEGY_RECOMMENDATIONS_READY → REPORT_READY → IMPLEMENTATION_PENDING → MONITORING_PENDING`

It also proves:

- a pre-consent handoff is rejected;
- duplicate questionnaire submission, handoff, property import, and result import do not create duplicates;
- the handoff is accepted by the GEO Engine adapter and resolves the GeoAIResume property;
- audit references and a company-identity-bound report are imported;
- monitoring is paused before verified implementation;
- withdrawal blocks a subsequent handoff.

## Files changed

Outreach Agent:

- `src/outreach_agent/research_lifecycle.py`: shared lifecycle constant, explicit local owner-fixture handoff protocol, and withdrawal gate.
- `docs/cross_repository_integration_report.md`: this report.

GEO Engine:

- `app/integration/lifecycle.py`: matching outreach-first lifecycle constant while preserving the existing engine lifecycle.
- `tests/test_cross_repository_owner_fixture.py`: deterministic cross-repository fixture test with mocked audit/provider/deployment behavior.
- `pytest.ini`: scopes pytest collection to `backend/tests`; the backend also contains manual `test_*.py` smoke scripts that are not pytest tests.

GeoAIResume:

- No source files changed. Its existing `integration/site.json`, content manifest, proposal validation, deployment receipt logic, and integration tests were used as the controlled fixture contract.

Pre-existing dirty files and generated datasets in all three repositories were not reset, deleted, or rewritten.

## Test commands and results

```sh
cd "/Users/melc/Documents/Intern/LLM Search Optimizer/geo-research-outreach-agent"
PYTHONPATH=src uv run pytest -q tests/test_integration_phase1.py tests/test_research_lifecycle.py tests/test_reporting.py
# 11 passed
```

```sh
cd "/Users/melc/Documents/Intern/LLM Search Optimizer/geo-engine/backend"
PYTHONPATH=. pytest -q tests/test_geo_research_integration.py tests/test_cross_repository_owner_fixture.py
# 9 passed
```

```sh
cd "/Users/melc/Documents/App Design/GeoAIResume"
npm run test:integration
# 8 passed
```

The direct model import is fast in isolation (`PYTHONPATH=. python3 -c 'import app.models'`, approximately 0.25 seconds in this environment). The broader `pytest --collect-only` invocation previously appeared to hang because collection included backend-root manual `test_db.py`/`test_reddit.py` files and then entered import-time side effects from experiment/teacher-pipeline test modules. `test_db.py` also fails when collected from `backend/` because it uses `backend.app` rather than the local `app` package. The safe mitigation added is `backend/pytest.ini` with `testpaths=tests`; no ORM, migration, model, or scientific experiment code was changed. Broad collection remains unsuitable for this repository until those import-time side effects are separately refactored; the targeted integration and contract suites do not exhibit the problem.

## Readiness for real operation

| Capability | Status | What remains |
|---|---|---|
| Real Gmail delivery | MOCKED / BLOCKED | Existing Gmail code is an allowlisted test-send path, not bulk outreach. A production sender, scoped OAuth/service identity, recipient approval, rate limits, bounce handling, audit trail, and owner authorization are still required. |
| Public questionnaire hosting | MISSING | Host an authenticated HTTPS form, publish versioned consent wording, bind invitations to a safe token/expiry policy, and retain submission evidence. |
| Authenticated response ingestion | MOCKED | Implement authenticated webhook/API ingestion, signature verification, replay/idempotency handling, schema validation, and mapping to the correct invitation/profile. |
| Reply tracking | PARTIAL / MOCKED | Advisory local reply classification exists. Real Gmail thread/watch ingestion, unsubscribe handling, human review, and record linkage are missing. |
| Real GEO Engine transport | MOCKED | The local adapter/store and schemas exist. A network endpoint, scoped authentication, TLS/retry policy, idempotency key transport, callback/polling resource, and operational observability are still required. |
| Paid LLM baseline execution | BLOCKED | The contract supports references, but no paid provider call was made. Credentials, budget approval, frozen question/source set, provider/model/prompt capture, and reproducible result persistence are required. |
| Approved website changes | BLOCKED | GeoAIResume proposal application requires owner approval and current content hashes. No proposal was approved or applied, and no deployment occurred. |
| Scheduled long-term monitoring | BLOCKED | Engine monitoring gates exist, but activation requires verified deployment, approved cadence/budgets, provider execution, scheduler ownership, withdrawal checks, and alerting. |

## Recommended next phase

Implement the authenticated questionnaire and result-transport boundary first, using the same fixture and idempotency keys. Then add a dry-run network contract against a local mock service. Only after that should the owner approve one paid baseline run and, separately, one proposal/deployment verification cycle.

