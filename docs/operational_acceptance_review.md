# Operational acceptance review: Outreach Agent → GEO Engine

Date: 2026-10-22

## Result

The local lifecycle and handoff controls are operational in isolated fixtures. The Google Sheets watcher now writes a non-sensitive JSON health file and has a launchd template with restart behavior and log paths. The Research Queue now exposes questionnaire responses, conflict quarantine, consent scope, review history, and explicit approve/reject controls.

The configured production GEO Engine database was not changed or used. Acceptance used a temporary GEO Engine SQLite database, temporary integration store, and temporary backend environment. The normal shell did not have `GEO_ENGINE_ROOT` or `GEO_ENGINE_INTEGRATION_STORE`; Outreach and GEO Engine `DATABASE_URL` values remained separate. This is an explicit deployment boundary, not a reason to write to a production database.

## Safety and lifecycle checks

- Distinct responses for one invitation remain quarantined and block handoff.
- Approval requires a human review decision plus identity, representative authorization, consent-version, website-scope, withdrawal, and invitation-email checks.
- Withdrawal and conflicting responses block later handoffs.
- Repeated approval/handoff attempts reuse the existing handoff and GEO Engine property mapping; no audit, experiment, monitoring, deployment, or paid job is started.
- Dashboard review writes are loopback-only by default. If the dashboard is ever changed to a trusted remote deployment, `OUTREACH_REVIEW_TOKEN` must be configured and sent as a bearer token or `X-Outreach-Review-Token`.

## Local commands

One-shot Sheets dry run:

```sh
cd "/Users/melc/Documents/Intern/LLM Search Optimizer/geo-research-outreach-agent"
PYTHONPATH=src uv run python scripts/sync_google_form_responses.py \
  --credentials "/Users/melc/.config/geo-research-outreach-agent/geo-sheets-sync.json" \
  --dry-run \
  --status-file /tmp/geo-research-outreach-agent-sheets-sync.status.json
```

Managed polling in the foreground:

```sh
PYTHONPATH=src uv run python scripts/sync_google_form_responses.py \
  --credentials "/Users/melc/.config/geo-research-outreach-agent/geo-sheets-sync.json" \
  --watch --interval-seconds 300 \
  --status-file /tmp/geo-research-outreach-agent-sheets-sync.status.json
```

The launchd template is `config/com.geo-research-outreach-agent.google-sheets-sync.plist.template`. Replace its explicit placeholders, review the paths, then install it under `~/Library/LaunchAgents/`; it is not installed automatically by this repository.

Local dashboard:

```sh
cd "/Users/melc/Documents/Intern/LLM Search Optimizer/geo-research-outreach-agent"
PYTHONPATH=src uv run python scripts/phase5b_dashboard.py --host 127.0.0.1 --port 8765
```

GEO Engine handoff requires the Outreach process to receive `GEO_ENGINE_ROOT` and, optionally, `GEO_ENGINE_INTEGRATION_STORE`; the GEO Engine backend must separately have its own `DATABASE_URL`, `OPENAI_API_KEY` placeholder/configuration, and installed backend requirements. The adapter only registers/reuses a Property; research execution remains a separate operator action.

Acceptance setup used:

```sh
uv venv /tmp/geo-engine-backend-venv --python 3.12
uv pip install --python /tmp/geo-engine-backend-venv/bin/python \
  -r "/Users/melc/Documents/Intern/LLM Search Optimizer/geo-engine/backend/requirements.txt"
```

The GEO Engine requirements now explicitly include `jsonschema`, which its versioned integration contract imports. For this Mac checkout, `PYTHONPYCACHEPREFIX=/tmp/geo-engine-pycache` avoids stale backend bytecode during local startup.

Example isolated environment variables:

```sh
export GEO_ENGINE_ROOT="/Users/melc/Documents/Intern/LLM Search Optimizer/geo-engine"
export GEO_ENGINE_INTEGRATION_STORE="/tmp/geo-engine-acceptance-store"
export DATABASE_URL="sqlite:////tmp/geo-engine-acceptance.db"  # GEO Engine process only
export APP_ENV=test
export OPENAI_API_KEY=local-acceptance-placeholder
```

Do not export the GEO Engine `DATABASE_URL` into the Outreach process when the Outreach registry is running; its registry path is controlled separately by the Outreach lifecycle command.

## Validation

- `node --check src/outreach_agent/dashboard/app.js`: passed.
- `uv run python -m py_compile scripts/sync_google_form_responses.py`: passed.
- `uv run pytest -q`: passed, 307 tests.
- launchd plist syntax: passed with `plutil -lint`.
- isolated GEO Engine adapter idempotency was previously verified: repeated import reused one property and one profile-import record.
- Isolated GEO Engine API health and property visibility passed on port `18000`: `/health` returned HTTP 200 and `/api/v1/properties` returned the single matching GeoAIResume property after handoff.

No email, form submission, external consent approval, production registry write, website audit, experiment, monitoring job, or paid evaluation was performed.
