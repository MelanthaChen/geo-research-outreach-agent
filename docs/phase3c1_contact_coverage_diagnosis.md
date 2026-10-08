# Phase 3C.1: Contact Coverage Diagnosis & Targeted Fixes

## Summary

The Phase 3C coverage gap had both real-world and fixable causes. A controlled refresh kept the same 77 company IDs and page/request settings. Website availability stayed at 58/77 fetched, but targeted page prioritization and evidence classification raised primary channels from 17 to 25 (+8). There are still 19 inaccessible websites, 9 companies with only unsuitable channels, and 20 accessible companies with no suitable channel found in the bounded fetched pages. No email was guessed, no form was submitted, and no anti-bot restriction was bypassed.

Phase 3C.1 added four evidence-backed software fixes: prioritizing contact/partnership/business-inquiry links within the existing page cap; recognizing explicit role mailboxes without treating generic email as partnership intent; matching visible personal email to a nearby named person using full name or first-initial-plus-surname; and correcting organization/person structured-data classifications. The website collector now saves failed page URLs/statuses instead of leaving only a log line. Human approval state remains untouched; recommendations do not imply approval.

## Before and after

| Metric | Phase 3C (77) | Phase 3C.1 (77) |
| --- | ---: | ---: |
| Website inspection fetched | 58/77 (57 success, 1 partial) | 58/77 (56 success, 2 partial) |
| Companies with any observed channel record | 29 | 34 |
| Companies with a suitable primary channel | 17 | 25 |
| Primary channels recommended | 17 | 25 |
| Partnership email primaries | 0 | 0 |
| General business email primaries | 12 | 12 |
| Contact-form primaries | 3 | 9 |
| Named-person email primaries | 0 | 2 |
| Only unsuitable channels | 12 | 9 |
| Website-level fetch failures/blocks | 19 | 19 |
| Confirmed ranking errors | 0 | 0 |
| Confirmed classification issues in reviewed evidence | 4 corrected | 0 remaining in the covered examples |
| Confirmed false-positive primary routes | Not measured | 0 confirmed; 1 program-specific form remains a review risk |

For strict `SUCCESS` websites, primary-channel coverage increased from 17/57 (29.8%) to 25/56 (44.6%). Counting partial crawls as fetched, it increased from 17/58 (29.3%) to 25/58 (43.1%). The 28-company sample held the same 28 IDs: fetched sites were 27/28 in both runs; primary channels increased from 6 to 10, and companies with any channel record were 18 before and 17 after. The lower “any channel” count in that smaller sample reflects unsupported/overbroad rows being removed or downgraded, not lower suitable coverage.

The 8 additional 77-company primaries break down as follows:

- Five surfaced after contact/business routes received page slots ahead of generic product/editorial pages: ACME Block & Brick, Actikare, Barge Design, Best Buy Metals, and Blood Assurance.
- Three of the new primaries resulted from correcting extraction/classification: Jcode’s explicit Person-schema email, All Ways Caring’s organization-schema email, and Light Anchor’s published `founders@` mailbox.

Bandy Heritage Center’s existing broad email candidate was reclassified as Erica Perez’s named-person email after matching `eperez11@daltonstate.edu` to the nearby “Contact Erica Perez” evidence. That improved accuracy but did not add a company to the coverage count. Together these examples account for four confirmed classification corrections; the other four newly recommended routes came from improved page selection and extraction of the newly fetched pages.

## Why the original 60 companies lacked a primary channel

The original 60-company gap is accounted for below. Categories are mutually exclusive and sum to 60.

| Phase 3C diagnosis | Companies | Interpretation |
| --- | ---: | --- |
| `FETCH_FAILED` | 19 | 12 blocked with HTTP 403; 7 transport failures (DNS, TLS/certificate, or timeout) |
| `ONLY_UNSUITABLE_CHANNELS` | 12 | Only support, restricted, or `OTHER` evidence; correctly withheld from primary recommendation |
| `NO_CONTACT_PAGE_DISCOVERED` | 21 | No contact-like route in the pages fetched under the bounded page budget; the old crawler did not persist its complete discovered-link queue |
| `NO_PUBLIC_CHANNEL_FOUND` | 5 | A contact route was fetched, but no suitable email/form was extracted |
| `INSUFFICIENT_EVIDENCE` | 3 | Two company/site identity mismatches and one partial crawl; not safe to infer no contact route |

The original report’s top-level 19 failures and 41 fetched-but-unrecommended sites are therefore consistent: among the latter, 12 had only unsuitable channels; 26 had no channel record (21 without a contact-like fetched route and 5 with a contact route but no suitable channel); and 3 needed evidence/identity review.

## Phase 3C.1 diagnostic categories (all 77 companies)

| Final category | Count |
| --- | ---: |
| `FETCH_FAILED` | 19 |
| `PRIMARY_CHANNEL_AVAILABLE` | 25 |
| `ONLY_UNSUITABLE_CHANNELS` | 9 |
| `NO_CONTACT_PAGE_DISCOVERED` | 12 |
| `NO_PUBLIC_CHANNEL_FOUND` | 8 |
| `CONTACT_PAGE_FETCH_FAILED` | 1 |
| `INSUFFICIENT_EVIDENCE` | 3 |
| `CHANNEL_EXTRACTION_MISSED` | 0 confirmed after fixes |
| `CHANNEL_CLASSIFICATION_ERROR` | 0 remaining in reviewed evidence |
| `CHANNEL_RANKING_ERROR` | 0 |
| `REVIEW_STATE_INCONSISTENCY` | 0 |

`NO_CONTACT_PAGE_DISCOVERED` describes only the bounded fetched-page record, not a claim that the site has no contact page. The Phase 3C database did not retain the full set of discovered-but-unfetched links. The new diagnostic CSV leaves `pages_discovered` blank for that reason and reports pages fetched separately.

The remaining 33 fetched companies without a primary route comprise 9 with only unsuitable channels and 24 with no suitable route established. Those 24 are mutually exclusive: 12 had no contact-like route in fetched pages, 8 had a fetched contact route but no suitable channel, one contact-page request was blocked, and three cases had insufficient evidence.

## Fetch-failure investigation

The 19 currently inaccessible websites are 12 HTTP 403 blocks and 7 transport failures: two DNS resolution errors, three TLS hostname/certificate errors, one TLS handshake failure, and one Alrol timeout/read failure (HTTP 500 was also observed during the 28-company refresh). The failures are not treated as proof that the company has no public contact information. The errors are a point-in-time observation; DNS, TLS configuration, 403 access policies, and server errors may change.

Two partial pages were observed in the 77-site refresh: American Carpet Wholesalers’ `/contact.html` returned 403 (no bypass attempted), and Drafted’s Cloudflare email-protection URL returned 404. Only the former is classified as `CONTACT_PAGE_FETCH_FAILED`; the latter is insufficient evidence, not a failed contact form. No repeat requests beyond the single bounded sample refresh were made.

## Evidence and human-review findings

- Six email routes whose mailbox domain differs from the company domain and four externally hosted forms are held for human review; in total, 10 primary candidates are `NEEDS_REVIEW` because of cross-domain email/form destinations. They are not automatically approved.
- The 25 primary candidates all remain pending human approval. Two additional `NEEDS_REVIEW` companies have no primary route because the site/company evidence did not support a safe match; no route was fabricated to fix the status.
- All 9 `OTHER`, 4 restricted, and 3 support channels in the 77-company inventory were excluded from primary recommendation. No privacy, accessibility, legal, abuse, security, support, newsletter, login, or job-application route was promoted.
- The externally hosted Blood Assurance form is titled “SBB Contact Form”; it may be program-specific rather than a general business route. It remains review-required. The other external forms also remain review-required because their destination host differs from the company site.
- No definite false-positive route was confirmed in the reviewed primary examples. This is not a measured precision estimate; the program-specific Blood Assurance form and all cross-domain routes are explicit review risks.
- No partnership-intent email was found in either sample. Generic `info@`, `hello@`, `team@`, and `founders@` mailboxes are labeled general business, not partnership, unless the evidence explicitly supports partnership intent.

## Crawl4AI and runtime

The 77-company Phase 3C.1 fallback ran 15 times, succeeded 15 times, fetched 23 pages, added 6 named contacts, and rejected 7 unsupported candidates; recorded fallback runtime was 7.67 seconds. This is lower than Phase 3C’s 19 runs because more named contacts were available from the improved first-party extraction. Crawl4AI was not needed for the 28-company run. Website requests remained sequential and bounded by the existing page budget and request delay. The 77-site refresh took about 8 minutes wall-clock; the 28-site refresh about 3 minutes, including one slow Alrol response.

## Files and verification

- `data/phase3c1_company_diagnostics.csv` — one row for every company, including source, fetch outcome, pages fetched, observed emails/forms, candidate/accepted/rejected channel counts, rejection reasons, primary route, review state, and diagnostic category.
- `data/phase3c1_validation_28_results.csv`
- `data/phase3c1_validation_77_results.csv`

The original Phase 3C report and result CSVs were preserved. Both controlled runs used the same IDs (28/28 and 77/77), the same configured crawler limits, and no new database/agent/CLI was introduced. The full test suite passes (59 tests); website/HTTP-only, Crawl4AI compatibility, and contact/idempotency tests pass. `git diff --check` is clean.

## Recommendation

The system is appropriate for a human-reviewed pilot, not unattended outreach. The 17→25 improvement is defensible: eight companies gained a primary route from page-priority or supported-classification fixes while fetch availability remained 58/77. The remaining gap is mostly real access limitations and absence of a suitable public route in the bounded pages, with some residual unknowns because discovered-but-unfetched URLs were not historically stored. Review external routes, especially the Blood Assurance program form, before use.
