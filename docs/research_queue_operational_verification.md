# Research Queue operational verification

Date: 2026-10-22

## Result

The Research Queue is now populated from the existing candidate stores through a safe, incremental adapter. The adapter reads `data/phase7_candidates.db` and `data/hotel_candidates.db` in SQLite read-only mode and writes only to the separate lifecycle registry at `data/research_registry.db`. It preserves source candidate IDs, cohort identity, source evidence, website, industry, location, qualification, and verification status. Re-running ingestion is idempotent and does not overwrite recruitment, questionnaire, consent, or GEO lifecycle state.

Current local registry snapshot:

| Measure | Count |
|---|---:|
| Real business records visible | 57,664 |
| Real hotel records visible | 15,262 |
| Total real records visible | 72,926 |
| Owner-controlled test records | 1 |
| Verified records with contact evidence | 13,482 (13,121 business; 361 hotel) |
| Outreach-ready records | 0 |
| Real records with preliminary GEO screening evidence | 0 |

The owner-controlled fixture is `https://geo-ai-resume.vercel.app`. It is not in the business or hotel cohorts, is excluded from normal recruitment, and is not present in the source candidate databases.

## Local startup and UI verification

Documented startup command:

```bash
uv run python scripts/phase5b_dashboard.py --host 127.0.0.1 --port 8765
```

For this verification, port 8765 was already occupied by the existing local dashboard, so the same application was run on port 8766 with:

```bash
OUTREACH_ENV=local \
RESEARCH_QUESTIONNAIRE_SECRET=local-test-secret \
RESEARCH_QUESTIONNAIRE_BASE_URL=http://127.0.0.1:8766 \
PYTHONPATH=src \
uv run python scripts/phase5b_dashboard.py --host 127.0.0.1 --port 8766
```

Dashboard URL used: [http://127.0.0.1:8766/#research-queue](http://127.0.0.1:8766/#research-queue). The existing dashboard process and hotel worker were not interrupted.

Verified locally:

- paginated queue: 25 records per page, 72,926 real records;
- search: exact company/property search returned one matching hotel record;
- cohort filtering: hotel filtering returned only `hotel_property` records;
- CSV export: returned the registry schema and filtered rows;
- record detail: encoded source IDs now resolve correctly and display consent, recruitment, questionnaire, GEO, screening, drafts, and handoffs;
- owner fixture: invitation, questionnaire, consent update, and local handoff readiness completed;
- no real email, form submission, deployment, paid LLM call, or external outreach occurred.

The queue displays lifecycle states separately. Records remain `NOT_SCREENED` and `NOT_READY` unless evidence and approvals exist; a discovered record is not treated as verified or contactable merely because it is present.

## Questionnaire verification

Invitation URLs now use `RESEARCH_QUESTIONNAIRE_BASE_URL`. In local mode, the loopback fallback is available for testing. Outside local mode, missing configuration raises an error instead of generating a localhost URL. Tokens are opaque HMAC-backed values with expiry, identity binding, duplicate-response idempotency, and conflicting-response review handling. Consent wording/version, timestamps, invitation identity, and response hashes are retained.

The local fixture reached `CONSENTED` and `QUEUED` only after identity confirmation and explicit website-analysis consent. The handoff is idempotent and uses the explicit `OWNER_CONTROLLED_TEST_LOCAL_ONLY` protocol.

## AI evidence and production blockers

No preliminary GEO screening evidence is currently available for the real imported records; all real records are `NOT_SCREENED`. Contact evidence is preserved as read-only source evidence and does not authorize outreach by itself.

Remaining production blockers:

- production requires a strong `RESEARCH_QUESTIONNAIRE_SECRET` and public HTTPS `RESEARCH_QUESTIONNAIRE_BASE_URL`;
- no production Gmail provider is enabled by this work;
- human review and approval are still required before any real recruitment action;
- Gmail reply synchronization and sender verification need production credentials and deployment configuration;
- the GEO Engine transport/queue consumer remains a separate integration dependency;
- real preliminary screening must be supplied by an approved screening process before records become outreach-ready.

## Tests

Commands and results:

```bash
PYTHONPATH=src uv run pytest tests/test_research_lifecycle.py -q
# 10 passed

PYTHONPATH=src uv run pytest -q
# 270 passed
```

The lifecycle tests cover owner-controlled exclusions, source-store ingestion, cohort separation, idempotency, contact evidence, token expiry, duplicate and conflicting questionnaire responses, simulated-send safeguards, and handoff consent gates. The full suite passed after the encoded detail-route fix and the handoff endpoint addition.

No source candidate database, crawler checkpoint, or hotel-worker configuration was modified.
