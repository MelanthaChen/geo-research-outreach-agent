# Phase 5A: Outreach Pipeline Validation

## Phase 5C update

The current invitation template is form-first: the Participation Interest Form is the primary call to action, while the research contact email is for questions only. The 5A run metrics below describe the original demo snapshot; the current offline demo uses a clearly labeled reserved `.invalid` form placeholder and simulates form open, interest indication, team review, and follow-up without external requests or actual submissions.

## Outcome

Phase 5A adds an offline, provider-free outreach preparation and professor-demo path against the existing 537-company Phase 4F experiment. It demonstrates evidence-backed drafts and a local simulated workflow while keeping real delivery and website-form submission unavailable. The frozen 77-company validation database was not opened for writes. No real messages, form submissions, or research signups occurred.

## Implementation

- `scripts/phase5a_outreach_demo.py demo` opens only `data/phase4f_experimental.db` in read-only mode, requires the existing 537-company cohort, uses saved qualification/identity/contact evidence, and deterministically exports email drafts, a form draft, candidate evidence, and simulated lifecycle rows.
- `DemoTransport` is the only transport in this phase and has no network code. It creates deterministic `SIMULATED-*` IDs and suppresses duplicate demo deliveries by an idempotency key.
- The default `config/outreach.yaml` leaves sender name/email, reply-to, participation form URL, research contact email, researcher name/team, and affiliation unset. `live_sending_enabled` is false. The delivery gate remains blocked in the absence of a real provider even if hypothetical future approval/configuration flags are set.
- Contact form mapping uses only saved visible form evidence, marks CAPTCHA/consent indicators, requires manual confirmation, and always returns `NOT_SUBMITTED_DEMO`.
- Fixture approval, Vexo delivery, interested response, and signup handoff are isolated as DEMO/SIMULATED. The `.invalid` destination is non-operational. Signup completion is false and research consent remains `NOT_ESTABLISHED`.
- Review state and suppression data are read only during the demo. Simulated responses do not persist opt-out/approval changes.
- `enrich-cache` reuses the existing extractor against cached snapshots only, with fallback disabled and batch size capped at 25. It modifies only contact records/contact processing status in the Phase 4F experimental database and asserts company/contact human-review fields remain unchanged. It is resumable by selecting only eligible, identity-verified `NOT_RUN` companies without suitable saved contact channels.

No Gmail, Microsoft 365, SMTP, API keys, sender credentials, browser automation, or live provider dependency is required. There is no real delivery adapter in Phase 5A. Sender/provider integration, sending authorization, real reply processing, form submission, and production signup handoff remain future work.

## Reproduction

Run the provider-free presentation path:

```sh
PYTHONPATH=src uv run python scripts/phase5a_outreach_demo.py demo --max-draft-companies 10
```

Expected counts:

```text
cohort=537 draft_companies=10 email_drafts=9 form_drafts=1
simulated_deliveries=1 simulated_interested_responses=1 simulated_signup_handoffs=1
real_sends=0 form_submissions=0 sender_configured=False
```

Key examples are Vexo (company ID 5, first-party business email), AnswerThis (ID 105, first-party contact form), and CodeWisp (ID 88, no suitable stored channel). Draft outputs are under `data/phase5a_*.csv`. The demo can be repeated; export files are byte-stable and the prior simulated event is reused.

The bounded cached-contact continuation, if intentionally desired, is:

```sh
PYTHONPATH=src uv run python scripts/phase5a_outreach_demo.py enrich-cache --limit 100 --batch-size 25
```

During this validation it processed 333 previously unprocessed eligible, verified companies over four invocations: **333 processed, 267 with a suitable channel found, 66 with no suitable channel found, and 473 contact records created**. Each run used batches no larger than 25. The command reported zero website/network requests, fallback disabled, and zero company/contact human-review field changes. These are observed local extraction outcomes from saved evidence, not deliverability or contact-quality guarantees. A subsequent invocation resumes from remaining eligible `NOT_RUN` companies.

## Validation evidence

- Before this work, the repository suite had 126 passing tests.
- The added Phase 5A tests cover sender defaults, template requirements, recipient validation, fail-closed real delivery approval, absence of a live provider, demo transport idempotency and failure recovery, suppression persistence boundaries, state transitions, safe form mapping/CAPTCHA/consent handling, interest-versus-consent, and full offline demo repeatability/database and approval-state preservation.
- The end-to-end demo used the existing experimental cohort and produced 9 email drafts, 1 form draft, 1 simulated delivery, 1 simulated response, 1 simulated signup handoff, zero real sends, and zero form submissions.
- An offline-contact pass over 333 companies created 473 records. It made no network calls and changed no human review fields.
- The demo outputs are regenerated twice to compare byte-for-byte. The full test suite and `git diff --check` are run as final checks below.

The original 77-company database must remain unchanged; its expected SHA-256 is `b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6`.

For the concise five-minute presentation script and exact expected sample rows, see [`professor_demo_walkthrough.md`](professor_demo_walkthrough.md).
