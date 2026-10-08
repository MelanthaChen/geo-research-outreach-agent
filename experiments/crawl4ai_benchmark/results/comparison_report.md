# Crawl4AI evaluation against the existing contact pipeline

Evaluation date: 2026-10-07  
Production baseline: commit `c91eabe`, 37 tests passing  
Crawl4AI: 0.9.4 on CPython 3.12.11

## Decision

**ADOPT_PARTIALLY**

Crawl4AI's cleaned Markdown materially improved deterministic named-person extraction on this sample, but its browser crawler did not improve crawl success, was roughly four times slower on successful pages, missed evidence that the current HTTP fetch retained, and adds a large dependency/browser footprint. It should not replace the current crawling layer based on this benchmark.

A future production experiment could add Crawl4AI-derived Markdown as an optional fallback for already-selected first-party about/team/staff pages when the lightweight pipeline finds no named people. The existing SQLite model, robots/rate controls, cache, URL selection, provenance, email extraction, review workflow, and HTTP crawler should remain authoritative. If that fallback later proves reliable on a larger sample, only HTML-to-visible-text/Markdown rendering for those pages—not the entire collector—would be a candidate for replacement.

## Installation and operational observations

The benchmark followed the official open-source installation flow: `pip install crawl4ai`, `crawl4ai-setup`, and `crawl4ai-doctor`. Crawl4AI's official documentation describes cleaned Markdown, CSS/XPath schemas, browser execution, and optional LLM extraction. See the [official repository](https://github.com/unclecode/crawl4ai) and [installation guide](https://docs.crawl4ai.com/core/installation/).

- Isolated environment: `/tmp/crawl4ai-benchmark-venv`; production `pyproject.toml` and `uv.lock` were unchanged.
- Python compatibility: installation and doctor test succeeded on Python 3.12.11.
- Resolved environment: 95 Crawl4AI packages before installing the local project package.
- Browser dependencies: Playwright Chromium 1243 / Chrome for Testing 153.0.8010.12, Chromium Headless Shell, and FFmpeg; setup also installed the Patchright equivalents. The benchmark did not enable stealth, Patchright, proxies, CAPTCHA handling, or anti-bot bypasses.
- Disk observation after setup: isolated virtual environment approximately 603 MB; shared Playwright cache approximately 2.4 GB. The cache may include prior browser assets, so 2.4 GB is an observed total rather than an incremental measurement.
- Import-only peak memory observed by `/usr/bin/time`: approximately 81 MB. Browser-process peak memory was not reliably attributable with the available process-level measurement and is therefore not claimed.
- No supported LLM provider key or local Ollama executable was configured. Approach C is `NOT_TESTED`; no paid API was called.

## Fixed sample

The benchmark used the four required known failures plus six deterministic additions from the frozen Phase 3 validation sample:

| Company | Source | Page |
|---|---|---|
| Vyra | YC | `https://www.usevyra.com/about` |
| Light Anchor | YC | `https://lightanchor.ai/about` |
| Honeylove | YC | `https://www.honeylove.com/pages/accessibility` |
| Alrol of America | Chamber | `https://alrolofamerica.com/about/` |
| Shire Intelligence | YC | `https://shireintelligence.com/contact-us` |
| Veeza AI | YC | `https://www.veeza.ai/en/about` |
| Jcode | YC | `https://jcode.sh/about` |
| Bench Builders | Chamber | `https://bench-builders.com/contact` |
| Big Brothers Big Sisters NW Georgia Mountains | Chamber | `https://bbbsngm.org/our-staff/` |
| Bruster's Dalton | Chamber | `https://brusters.com/locations/dalton/349/` |

The run was sequential, slept one second between requests, enabled Crawl4AI's robots check, used ordinary headless Chromium, and fetched only the specified first-party page. Empty and failed cases remain in the result files.

## Approaches

### A — existing extractor

The current production `extract_contact_evidence` function was run unchanged against a normal HTTP response for each exact URL. This measures the existing retrieval/extraction behavior without touching SQLite.

### B — Crawl4AI deterministic

Crawl4AI rendered each URL and returned cleaned HTML plus Markdown. A site-independent benchmark extractor used only narrow evidence patterns:

- exact full-name lines adjacent to explicit role lines;
- explicit “Name, co-founder”, “founder, Name”, and “I'm Name” statements;
- meaningful image-alt evidence containing a name and founder role;
- explicit emails, kept generic unless a nearby full name and personal local-part agree.

Broad capitalized-phrase matching was rejected after a pilot produced obvious false people such as cookie/vendor labels. No company-specific selector was added. CSS/XPath schema extraction was inspected but not scored: the ten pages did not share a stable person-card selector, and separate per-company schemas would violate the no-company-specific-hacks requirement. The useful Crawl4AI capability here was its structure-preserving Markdown, not a universal built-in contact schema.

### C — Crawl4AI with LLM

`NOT_TESTED`. Crawl4AI supports provider-backed structured extraction, including local Ollama through LiteLLM, but no suitable no-cost provider was configured. A valid test would require an already-available local model or explicitly authorized API key, a strict schema, a non-invention prompt, and the same evidence verification used here.

## Measured results

Ground truth contains 15 confirmed named people across the ten pages. The compound “Trey & Lauren Tucker” label on Bruster's was marked uncertain and excluded from named-person precision/recall.

| Metric | A: Existing | B: Crawl4AI deterministic |
|---|---:|---:|
| Crawl success | 9/10 (90.0%) | 9/10 (90.0%) |
| Relevant-page retrieval | 9/10 (90.0%) | 9/10 (90.0%) |
| Named-person recall | 0/15 (0.0%) | 9/15 (60.0%) |
| Named-person precision | N/A (no predictions) | 9/9 (100.0%) |
| Role accuracy among named predictions | N/A | 8/9 (88.9%) |
| Company-association accuracy | N/A | 9/9 (100.0%) |
| Unique published email evidence accuracy | 5/5 (100.0%) | 6/6 (100.0%) |
| Generic-mailbox semantic accuracy | 4/5 (80.0%) | 5/6 (83.3%) |
| Confirmed false named people | 0 | 0 |
| Confirmed named-person false negatives | 15 | 6 |
| Median processing time/company | 0.164 s | 1.053 s |
| Mean time, successful pages | 0.249 s | 1.049 s |
| Mean time including timeout | 3.256 s | 4.158 s |

The single generic semantic error in both approaches was Shire's `alex@...` address initially represented as generic. Approach B additionally emitted a correct named association for Alex Tabaku but retained the redundant generic record. This is not counted as a false named person, but it needs deduplication before any integration.

Approach B's named false negatives were the three Alrol leaders (crawl failure) and three of four BBBS staff members. Email false negatives included Light Anchor's shared founders mailbox and all three Alrol personal addresses. The existing extractor missed all 15 named people.

## Known-case analysis

### Vyra

**Yes.** Crawl4AI retrieved structured Markdown for both explicitly listed founders:

- `Sulan Zhang, co-founder. Painter. On leave from Brown.`
- `Caleb Pong, co-founder.`

Approach B returned both names with `FOUNDER_OWNER` and preserved the surrounding evidence URL. It also retained `sulanzhangart@gmail.com` as generic/unassociated rather than attaching it to either person. Approach A returned no contact from this page.

### Light Anchor

**Yes, with one role-association defect.** Crawl4AI preserved:

- `Sangha Park` followed by `Co-Founder · ex-Sendbird Lead Product Manager`
- `Chase Kim` followed by `Co-Founder · ex-Sendbird Head of Forward Deployment`

Approach B found both co-founders. Sangha's role was accurate; Chase was incorrectly paired with Sangha's extended role text, although the normalized `FOUNDER_OWNER` role remained correct. Crawl4AI's Markdown omitted the shared `founders@lightanchor.ai` address that Approach A extracted from HTML.

### Honeylove

**Yes.** Both approaches returned only `accessibility@honeylove.com` as a generic mailbox. Neither interpreted “Honeylove Accessibility Statement” or “3rd-party Controlled Content” as a person. The mailbox is explicit but low-suitability for research outreach.

### Alrol

**No in this run.** Both the normal HTTP request and Crawl4AI timed out on the page. Crawl4AI therefore extracted none of the three confirmed leaders. This demonstrates no retrieval advantage for this known failure and prevents a fair extraction comparison on the live response. The manual ground truth retains the first-party evidence previously verified for Andreas Bruhwiler, Daniela Bruhwiler, and Jack Flowers.

## Retrieval versus extraction

Nine pages were reachable by both approaches. The large named-person gain came primarily from a deterministic extractor using Crawl4AI's line-preserving Markdown, not from Crawl4AI reaching pages the lightweight crawler could not reach. Crawl4AI did expose image-alt evidence effectively for Bench Builders, but it also omitted Light Anchor's `mailto:` address and did not overcome Alrol's timeout.

This distinction is the central reason not to replace the current crawler. A smaller HTML-to-structured-text improvement may be sufficient to gain much of the recall without importing a browser runtime into every crawl.

## Safety and production status

This evaluation did not modify production modules, SQLite schema, qualification, company/contact records, or sending behavior. It did not use LinkedIn content, search scraping, logins, CAPTCHA bypasses, external enrichment, guessed emails, or paid APIs. Crawl4AI remains an isolated experiment; no production integration was implemented.

## Final recommendation details

If a larger follow-up confirms the result, integrate one optional rendering interface into the existing `WebsiteCollector` rather than creating a second crawler or agent:

1. Keep current URL selection, robots checks, rate limits, response caps, cache, and provenance.
2. Invoke browser rendering only for high-value first-party pages whose lightweight extraction produces no named contact and whose HTML indicates dynamic/structured content.
3. Feed Crawl4AI Markdown into a separately tested evidence extractor.
4. Apply the current contact normalization, company-association checks, ranking, review, and SQLite persistence exactly once downstream.
5. Never enable stealth/proxy/anti-bot features.

No old production code should be removed now. Only after a larger benchmark should the `extract_page` visible-text conversion potentially be replaced or supplemented; `WebsiteCollector`, contact safety rules, and persistence should remain.
