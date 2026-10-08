# Phase 5B: Professor Demo Dashboard

## What it is

A small, local dashboard over the existing Phase 4F experimental cohort. It reuses the Phase 5A invitation template, channel rules, and simulated workflow; it does not create another discovery or qualification pipeline. The interface reads the database and saved verification/delivery CSVs. Interactive simulation events exist only in the open browser page and are discarded when it reloads.

The header permanently displays **DEMO MODE**. No provider, SMTP client, outbound website crawler, form-submission endpoint, or approval-write route is present. The server binds to loopback only. Its SQLite connection uses `mode=ro`, and the only HTTP routes are read-only GETs and static files; POST requests are rejected. Company and contact review decisions are never modified.

## Installation and startup

From the repository root, install the project and its test dependencies:

```sh
uv sync --extra dev
```

Start the local interface with one command:

```sh
uv run python scripts/phase5b_dashboard.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in a browser. Stop the server with `Ctrl+C`. It uses only local data and system fonts; it needs no API keys, sender account, signup URL, network access, or separate frontend build. The default database is `data/phase4f_experimental.db` and is deliberately restricted to that isolated 537-company cohort. Optional read-only input overrides are available with `--database`, `--verification`, and `--delivery-events`; a database named other than `phase4f_experimental.db` is rejected.

## Five-minute walkthrough

1. **Overview (0:00–0:45):** Point out the DEMO MODE marker. Review the stored 537-company count, identity and qualification distributions, first-party channel coverage, pending company/channel reviews, and the separate simulated-versus-real send counts. Values are calculated from SQLite and the saved Phase 4E/F identity and Phase 5A event exports. Where legacy baseline companies have no export row, the existing identity verifier evaluates their saved snapshots locally.
2. **Find a company (0:45–1:30):** Search by name, URL, industry, or location; combine source, identity, eligibility, GEO, priority, and channel filters. Choose a result. The table is capped visually at 250 rows at once; filters/search narrow it.
3. **Inspect evidence (1:30–2:30):** Review the selected company’s source records, saved qualification evidence/reasons, GEO rationale, missing evidence, contact-channel source excerpt, and company/channel review state. Emphasize that machine `VERIFIED` or `ELIGIBLE` labels are not human approvals.
4. **Preview (2:30–3:20):** Choose an existing suitable first-party channel for an identity-verified eligible company and generate the invitation preview. The actual saved email or official form URL and evidence source are shown. Sender values remain visibly unconfigured. The preview says **DEMO DRAFT — NOT SENT** and is not approved.
5. **Simulate (3:20–4:40):** Click **Simulate delivery**, then choose the simulated action to open the participation form and submit interest, decline, or give no response. The interested path demonstrates a form-open event, a simulated interest indication, pending research-team review, and the start of a simulated follow-up process. These are browser-memory-only fixture interactions; no external URL is opened, no form is submitted, no database event is written, and no follow-up message is sent. The displayed `.invalid` URL is reserved/non-operational; interest is not formal research consent.
6. **Close (4:40–5:00):** Explain that the experiment ends at the boundary: no real company was contacted, no form was submitted, no approval was created, and no consent/signup was recorded.

## Real versus simulated

| Dashboard element | Source / meaning |
| --- | --- |
| Company identity, qualification, GEO, priority, provenance, review status | Stored values from the Phase 4F experimental SQLite database and saved verification exports |
| Contact channels, recipients, contact forms, and evidence excerpts | Stored extraction records; not a verification of current deliverability and not authorization to use the channel |
| Existing simulated send/response/handoff counters | Rows in the saved Phase 5A simulation CSV; they do not represent real outreach |
| New interactive draft | Rendered from the configured invitation template and selected stored channel; includes a primary Participation Interest Form CTA, a question-only contact address, and warnings for unset configuration; ephemeral, unapproved, and not sent |
| New delivery and participation workflow in the walkthrough | Browser-memory-only demonstration fixture; not saved and does not alter human review or outreach tables. Form open/submission, team review, and follow-up are simulated; no real signup, consent, or contact occurs. |
| Real sends | Counted from the canonical outreach table. Phase 5A has no real provider and the cohort currently has no real outreach records. |

## Known limitations

- The UI is a local research demonstration, not an authenticated multi-user service. Do not expose it beyond loopback or use it as an operational outreach tool.
- Summary calculations are a startup snapshot; restart to load a changed source database or CSV. A browser refresh does not reload the server-side data snapshot.
- The website identity distribution uses saved Phase 4E/4F verification exports; where the baseline has no export row, the existing identity verifier evaluates only that company’s saved snapshot locally. This does not recheck the current live website or imply current reachability.
- Contact channel coverage means a suitable first-party channel is present in stored evidence. It does not prove deliverability, current ownership, or permission to contact.
- Evidence/rationale fields are presented as stored; some older records have missing or free-text evidence. The dashboard surfaces those gaps instead of filling them.
- Simulation state is intentionally not persistent. No screenshot is included because the dashboard runs from the user's local cohort; the interface can be previewed with the startup command.

## Safety validation

The tests check metric reconciliation against SQLite, filtering/search, company evidence details, evidence-backed draft rendering, missing-channel behavior, server write rejection, repeatable read-only loading, human-review/database preservation, and absence of external dashboard asset dependencies. The frozen 77-company database must retain SHA-256:

```text
b2313f4094eeba23ea6521ce78b39029b7140a828ef7d142b98de899284056b6
```
