# Phase 7: Contactable GEO Research Company Database

Phase 7 reuses the existing candidate database, identity verifier, GEO qualification rules, crawler, contact parser, and Phase 6G contact-review sidecar. It does not introduce a second qualification model, change campaign state, send email, or submit contact forms.

## Strict count definition

A company counts only when all available evidence supports: a verified unique canonical-domain identity; a successful first-party page fetch with substantive visible content; `ELIGIBLE` under existing GEO rules; and a same-domain public business email or an official form with visible input/submission controls. Generic contact pages, named-person records, support/privacy/legal/press role inboxes, unresolved chain/repeated-name records, rejected contact decisions, failed/blocked/robots-denied sites, and unverified companies do not count. Contact review approval and outreach authorization are separate; the export always records `NOT_GRANTED` for outreach authorization.

The operational signal is bounded: a successful current website response and substantive content. It is not proof of legal registration, active sales, or revenue. Stale evidence dates remain visible for human review.

## Reconciliation and execution

The existing Phase 6E source pool has 57,664 distinct canonical domains: 57,127 OSM candidates and 537 records matching the active 537-company cohort (447 YC public directory, 30 YC consumer directory, 60 Greater Dalton Chamber). The 537 domains are included in that total, not added twice. Phase 6F/6H checkpoints are retained as read-only inputs; Phase 7 creates its own one-time SQLite continuation copy at `data/phase7_candidates.db`.

Inspect the baseline and prepare the deterministic full-pool worklist:

```bash
PYTHONPATH=src uv run python scripts/phase7_contacts.py preflight
PYTHONPATH=src uv run python scripts/phase7_contacts.py prepare
```

The work order puts Phase 6E `LIKELY_FIT` candidates first, then records without chain/repeated-name or website-pattern flags, then higher existing Phase 6E priority, with canonical domain as a stable tie-breaker. This is prioritization only; qualification thresholds are unchanged. All never-attempted records in the pool are snapshotted, not only the Top 5,000.

Run or resume bounded work:

```bash
PYTHONPATH=src uv run python scripts/phase7_contacts.py run --batch-size 500 --request-cap 10000 --time-cap-seconds 21600 --chunk-request-budget 500
PYTHONPATH=src uv run python scripts/phase7_contacts.py status
PYTHONPATH=src uv run python scripts/phase7_contacts.py export
```

Request and time caps are cumulative across saved Phase 7 runs. Increase them to continue; the stable selection, saved website snapshots, and terminal statuses are reused. Candidate verification is serial, bounded, robots-aware, and uses the established timeout and retry handling. A completed 500-candidate interval pauses the run if identity verification or GEO eligibility drops below half the observed Phase 6F pilot rates. No CAPTCHA, bot protection, robots restriction, or access control is bypassed.

Before new candidate processing, Phase 7 recovers first-party parser channels already present in saved verified Phase 6E/6A website snapshots into the Phase 7 copy. This is offline, idempotent, leaves source databases unchanged, and marks new contact records pending. Website contacts are not refreshed unless the existing verifier is processing a never-attempted candidate.

## Outputs

- `data/phase7_candidates.db` — resumable continuation copy with saved selection and run ledger.
- `data/phase7_contactable_companies.csv` — one row per qualifying canonical domain.
- `data/phase7_contact_channel_audit.csv` — qualifying channel evidence and provenance.
- `data/phase7_validation_failures.csv` — bounded failure categories and safe next actions.
- `data/phase7_exclusion_queue.csv` — nonqualifying pool with explicit reason(s).
- `data/phase7_yield_metrics.csv` — measured yield, requests, retries, and runtime per 500 selected candidates.
- `data/phase7_final_report.md` — generated current totals and resumable command.

## Human controls and limitations

Contact channels remain pending unless the existing Phase 6G review sidecar records a decision. An approved contact channel is not research authorization or permission to send. No candidate is marked outreach-ready by Phase 7. The source pool is not exhausted, so Phase 7 does not acquire another dataset. The 5,000-company goal is not considered met by candidate counts, crawls, contact pages, or attempted requests—only the strict company export count qualifies.
