# Phase 7 overnight full-pool worker

Phase 7 can run as a per-user macOS LaunchAgent, independently of a Codex session or terminal window. It reuses `data/phase7_candidates.db`, the existing Phase 7 OS run lock, identity/GEO/contactability rules, saved evidence, and human-review states. It performs no email sends and no form submissions.

## Install and operate

Run from the repository root. The project virtual environment must already exist (`uv sync`). Installation writes a LaunchAgent to `~/Library/LaunchAgents/com.geo-research-outreach-agent.phase7.plist`, loads it in the current user's `gui/<uid>` launchd domain, and starts it:

```sh
PYTHONPATH=src uv run python scripts/phase7_service.py install
PYTHONPATH=src uv run python scripts/phase7_service.py status
PYTHONPATH=src uv run python scripts/phase7_service.py stop
PYTHONPATH=src uv run python scripts/phase7_service.py start
PYTHONPATH=src uv run python scripts/phase7_service.py resume
```

`stop` requests a graceful stop after the current saved 25-candidate chunk, waits up to five minutes, then unloads the LaunchAgent. It does not kill a worker if the checkpoint wait times out. `start` resumes with the installed caps. `resume` is for an exhausted cap or a stopped worker: it raises the cumulative hard caps by 100,000 HTTP attempts and 24 hours, rewrites the plist, and starts from the checkpoint. It does not restart a worker that is already running. `status` is read-only and reports pool/selection counts, processed and remaining candidates, strict contactability, email/form coverage, HTTP attempts, cumulative processing rate, the last saved chunk, service/worker PIDs, last error, integrity, caps, and log paths.

The launchd job runs the virtual-environment interpreter, sets `PYTHONPATH` to the repository's `src`, uses concurrency 4 and a 500-request maximum per internal chunk, and starts at the next login after a system restart. Its initial cumulative caps are **250,000 HTTP attempts** and **172,800 seconds (48 hours)**. If either cap is exhausted before the selected pool is complete, the job exits safely; use `resume` to increase both caps and continue. The existing pipeline may also pause if a completed 500-candidate interval falls below its established identity/GEO quality guard or if it cannot make progress; inspect `last_stop_reason` before resuming. Unsuccessful exits are relaunched by launchd, but a persisted guard stops after three restart attempts without a new saved processing chunk. Successful completion, an explicit operator stop, a hard-cap stop, or a quality-degradation pause does not trigger an automatic restart loop.

The job is wrapped in `/usr/bin/caffeinate -ims` while it runs. This temporarily prevents idle/system/disk sleep without changing `pmset` settings, while still allowing the display to sleep; the holds end with the worker. A LaunchAgent runs only in the user's login session. After a reboot, log into the same macOS account for `RunAtLoad` to resume it. The Mac should remain on AC power for an overnight run.

## Logs, state, and exports

- Standard output: `logs/phase7_worker.log`
- Errors: `logs/phase7_worker.err.log`
- Service status/restart guard: `data/phase7_service_state.json`
- Graceful-stop request: `data/.phase7_stop.request`
- Main checkpoint and cumulative request ledger: `data/phase7_candidates.db`
- Incremental exports refresh after each completed 500-candidate selection batch; a final refresh happens when a run stops or completes.

The full-pool exports contain one row per unique company in the primary contactable file and retain provenance/evidence and review status:

- `data/all_processed_candidates.csv`
- `data/all_verified_companies.csv`
- `data/all_geo_eligible_companies.csv`
- `data/all_contactable_companies.csv`
- `data/business_email_contacts.csv`
- `data/official_contact_forms.csv`
- `data/pending_human_review.csv`
- `data/full_pool_execution_report.md`

Existing `phase7_*.csv` exports remain available and continue to be regenerated.

## Qualification and safety

The strict automated count requires verified company identity, substantive first-party operating evidence, existing GEO `ELIGIBLE` qualification, and a matching suitable public business email or a confirmed first-party form. Generic contact pages do not qualify. Branch/franchise identity concerns, unknowns, failed/blocked/robots-denied pages, and unsuitable role inboxes are not promoted. Contact evidence remains pending human review unless there is an existing human decision; automated processing never grants outreach approval.

At installation on 2026-10-16, the reconciled live database contained 57,664 candidate rows/domains, of which 55,911 were in the deterministic Phase 7 selection. It had 6,354 selected candidates processed, 49,557 selected candidates pending, 1,341 strictly contactable companies, 29,104 HTTP attempts, and 21,378.46 seconds of unioned cumulative run time. Current counts should be obtained from `status`, not copied from this dated snapshot.

During the transition from the prior foreground run to launchd, the process was interrupted while the next 25-candidate chunk was in flight. Candidate outcomes are committed individually, so already-terminal candidate rows remain complete and will not be selected again. The aggregate HTTP ledger is committed when a chunk returns; requests made in that interrupted in-flight chunk cannot be reconstructed from persisted records. Therefore 29,104 is the persisted request count at service start and may undercount actual attempts by up to the 500-request chunk budget. The chosen cumulative hard cap leaves substantial headroom for this bounded accounting gap; no request count has been fabricated.

The AC power profile reported by `pmset -g custom` has display sleep at 60 minutes and system sleep at 1 minute; the machine was attached to AC power at inspection. `caffeinate` holds sleep while the service is active, so no persistent system power setting was changed. `pmset -g batt` and `pmset -g custom` can be used to re-check power state.
