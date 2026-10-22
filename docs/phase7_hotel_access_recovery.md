# Phase 7 Hotel Access Diagnosis and Recovery

Snapshot: 2026-10-22 04:16 UTC. The hotel worker is independent from the business worker. No emails were sent and no forms were submitted.

## Diagnosis at the 1,000-inspection checkpoint

The database was intact (`PRAGMA integrity_check = ok`), with 15,262 property candidates and 2,026 website domains. The 1,000 inspected properties contained 253 identity-verified hotels, 243 GEO-eligible hotels, and 135 properties with at least one saved business email or confirmed first-party contact form. The previous 591-inspection snapshot (161 verified, 154 eligible, 83 contactable) had grown to those 1,000-property totals before this recovery.

There were 253 `ACCESS_BLOCKED` outcomes and 137 `ROBOTS_DENIED` outcomes. Their HTTP evidence separates as follows:

| Outcome | HTTP 403 | HTTP 429 | robots.txt disallow without HTTP denial | Total |
|---|---:|---:|---:|---:|
| `ACCESS_BLOCKED` | 250 | 3 | — | 253 |
| `ROBOTS_DENIED` | 97 | 30 | 10 | 137 |

The 130 `FETCH_FAILED` outcomes comprised 33 DNS/connection-refused failures, 32 response-size-limit failures, 21 TLS/certificate failures, 15 HTTP 404s, 10 timeouts, 9 transient HTTP 5xx responses, 6 non-HTML responses, 3 invalid URL errors, and 1 parser rejection. HTTP 404s and redirects were preserved as evidence; an HTTP success was never sufficient by itself for identity verification.

### Breakdown by source and chain signal

| OSM extract | Inspected | Access blocked | Robots denied | Verified | Verified rate |
|---|---:|---:|---:|---:|---:|
| US Northeast | 426 | 96 (22.5%) | 28 | 138 | 32.4% |
| California | 272 | 57 (21.0%) | 43 | 74 | 27.2% |
| Texas | 174 | 57 (32.8%) | 37 | 30 | 17.2% |
| Georgia | 128 | 43 (33.6%) | 29 | 11 | 8.6% |

The imported category field was `hotel` for every inspected record, so these data cannot support a finer hotel-subcategory comparison. Chain signal did materially separate outcomes:

| Candidate class | Inspected | Access blocked | Robots denied | Verified | Verified rate |
|---|---:|---:|---:|---:|---:|
| Chain indicated | 491 | 168 (34.2%) | 98 (20.0%) | 55 | 11.2% |
| Independent or unknown | 509 | 85 (16.7%) | 39 (7.7%) | 198 | 38.9% |

There were 609 distinct inspected canonical domains. The three highest-volume blocked domains were `hilton.com` (46 blocks), `marriott.com` (44), and `choicehotels.com` (41): 131 blocked inspections, or 51.8% of all access blocks. Fourteen domains had multiple blocked properties. The old selector enforced one domain per *batch* but selected another sibling property on that domain in later batches; this explains repeated chain-domain blocks. The last batch was 11/25 blocked (44%) and correctly tripped the unchanged 40% circuit breaker. Across all 40 checkpoint batches, the median access-block rate was 24%; the mean was 25.3%.

Of the 1,000 records, 206 were `AMBIGUOUS_IDENTITY` and 5 `REJECTED_MISMATCH`: page evidence existed but did not safely establish the property/site link, or positively contradicted it. There were 137 observed cross-host redirects in saved page evidence. Examples include legacy property URLs redirecting to a chain-wide host, a booking aggregator, a domain-sale page, or unrelated repurposed content. Those remain ambiguous, rejected, or blocked as supported by their evidence; blocked sites were not labeled incorrect identities.

## Changes and safety behavior

The selector now:

- suppresses any previously `ACCESS_BLOCKED` or `ROBOTS_DENIED` host and hostnames beneath a blocked final redirect host;
- keeps one canonical domain per batch;
- allocates three selection turns to independent/unknown properties for each chain-indicated turn, reflecting the measured 38.9% versus 11.2% verification yields; chain properties are still sampled;
- retains the original candidate rows, property IDs, provenance, and review states. Suppressed candidates remain pending and visible; no identity is merged based on a shared domain.

The collector does not retry HTTP 403/429 responses. Instead, the denied host is withheld from future automatic selection, which avoids needing to sleep through a `Retry-After` window and is stricter than retrying after a cooldown. Robots denials are likewise not retried. Transport retries remain bounded to the existing single retry. The 40% batch circuit breaker remains enabled.

The first five resumed batches after the 1,000 checkpoint had access-block rates of 20%, 16%, 12%, 4%, and 16%. After checking for redirect-host sibling repeats, the worker was safely stopped at that batch boundary and reloaded with final-host suppression. It resumed from 1,125 without resetting or re-running completed properties. One full post-reload batch completed at 2/25 access-blocked (8%). A second post-reload batch was in progress at snapshot time.

## Official-website discovery pilot for candidates missing URLs

There were 6,768 records with no official website URL. The only local PBF available for a property-level scan was the 1.7 GB US Northeast extract. A read-only scan matched 2,267 no-URL candidate records from that extract and found 83 explicit OSM `wikidata` property links (3.7%). It did not discover URLs from OSM itself.

A separate, capped discovery tool was added at `scripts/phase7_hotel_website_discovery.py`. It accepts up to 25 exact OSM-property→Wikidata QID links, reads only the official-website (`P856`) claim from each linked entity, compares the entity label to the property name, rejects booking/directory domains, stores raw response evidence, and—when explicitly passed `--apply`—adds accepted URLs only as `DISCOVERED_UNVERIFIED` with separate provenance. It does not merge hotels sharing a chain domain.

Reproduce the bounded non-mutating pilot with:

```bash
PYTHONPATH=src uv run python scripts/phase7_hotel_website_discovery.py pilot --limit 25 --apply
```

The actual 25-QID pilot made one bounded request to Wikidata’s entity API and received HTTP 403, with no `Retry-After`. No response data or website URLs were retrieved, no sites were verified, and no records were applied to the hotel database. No alternate endpoint or bypass was attempted. The first request occurred before the tool’s blocked-source snapshot handling was added; therefore there is no successful snapshot or generated pilot CSV from that attempt. Future blocked responses are recorded as a failed snapshot and zero-yield CSV. Wikidata structured data is published under CC0, and its entity API is documented by Wikimedia; the OSM editing API was deliberately not used for this read-only collection because OSM directs read-only/large users to data downloads and alternatives ([OSM API usage policy](https://operations.osmfoundation.org/policies/api/), [Wikidata data access](https://www.wikidata.org/wiki/Help%3AData_access), [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata%3ALicensing)).

## Current execution snapshot

At 2026-10-22 04:16 UTC:

| Metric | Current | At checkpoint | Net change |
|---|---:|---:|---:|
| Inspected | 1,172 | 1,000 | +172 |
| Identity verified | 330 | 253 | +77 |
| GEO eligible | 317 | 243 | +74 |
| With any saved email/form evidence | 165 | 135 | +30 |
| `ACCESS_BLOCKED` | 272 | 253 | +19 |
| `ROBOTS_DENIED` | 150 | 137 | +13 |
| Pending website verification | 7,310 | 7,482 | −172 |

Six complete post-checkpoint batches plus one partial batch produced 172 saved property outcomes at this snapshot. The six complete batches used 443 additional HTTP attempts (state ledger 3,248 versus 2,805 at pause). The per-run table sums to 3,322 requests across 46 completed batches; the 40 pre-checkpoint run rows sum to 2,879, 74 more than the checkpoint state ledger. This 74-request discrepancy predates this recovery and has been preserved rather than rewriting the checkpoint; it is within the unchanged 25,002 cap. The partial batch was checkpointing property outcomes; its request total had not yet been folded into either cumulative total. The latest completed batch was 2/25 access-blocked, below the 40% circuit breaker.

The hotel LaunchAgent is running as PID `12710`, with the existing cumulative limits unchanged: 25,002 requests and 172,802 seconds. SQLite integrity is `ok`. Contact channels remain pending human review; no outreach readiness or approvals were changed. Website-discovery results did not alter the database.

## Tests and resumption

The full pytest suite passed after the discovery tool and selector changes; focused hotel and discovery tests also passed after final-host suppression. A scoped `git diff --check` over touched code/tests passed. The unrestricted repository-wide check still reports trailing whitespace on four rows in the pre-existing modified `data/phase7_contact_channel_audit.csv`; that historical evidence export was intentionally not rewritten.

The worker continues automatically under its finite existing LaunchAgent caps. If it exits at a future cap or quality pause, resume the saved checkpoint without raising those caps with:

```bash
PYTHONPATH=src uv run python scripts/phase7_hotels.py resume --request-increment 0 --time-increment-seconds 0
```

The worker is not claimed to continue after the LaunchAgent or host itself stops.
