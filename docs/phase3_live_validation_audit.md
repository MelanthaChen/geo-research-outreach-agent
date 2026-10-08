# Phase 3 live validation audit

Audit date: 2026-10-07

## Scope and method

This audit tested the Phase 3 contact-discovery system on 28 real companies already present in the 77-company Phase 2 dataset. It did not use LinkedIn, search-engine scraping, enrichment vendors, guessed addresses, logins, anti-bot bypasses, or outreach.

The sample contains 14 YC companies and 14 Greater Dalton Chamber companies. Within each source it balances `HIGH` and `MEDIUM` priority where possible, selects across the available industry strata, and uses company ID as the deterministic tie-break. The exact sample and selection rule are in `data/phase3_validation_sample.csv`.

The audit ran the normal bounded website collector with `--refresh`, then the existing `enrich-contacts --priority HIGH,MEDIUM` workflow. The pre-fix results were frozen in `data/phase3_validation_initial.csv` before code changed. Every result was checked against its cited first-party page; judgments and notes are in `data/phase3_validation_manual_audit.csv`.

## Initial results

| Measure | Companies | Rate |
|---|---:|---:|
| Sample | 28 | 100.0% |
| Automated `CONTACT_FOUND` | 14 | 50.0% |
| Defensible named contact | 0 | 0.0% |
| Defensible named-person email association | 0 | 0.0% |
| Usable generic/unassociated mailbox | 10 | 35.7% |
| Incorrect result | 2 | 7.1% |
| No reliable contact | 16 | 57.1% |

“Contact found” materially overstated quality. All 14 found companies had a syntactically valid public address, but only 10 had a mailbox judged usable for later human outreach review. None had a defensible named-person recommendation as represented by the system.

The two incorrect company-level results were:

- Honeylove: an accessibility statement heading and a “3rd-party Controlled Content” heading were mislabeled as a person and role. The address itself belongs to Honeylove, but is a poor research-partnership route.
- Artistic Civic Theatre: `actdalton.org` now serves an unrelated gambling site. Its admin address was falsely associated with the theatre.

Additional extraction failures included an FAQ heading labeled as a person at Pops, a paragraph labeled as a role at Alrol, shared mailboxes attached to nearby page headings, and missed explicit people on Vyra, Veeza AI, Jcode, Bench Builders, BBBS, Bruster's, and Light Anchor pages.

## Failure-mode findings

- **Website/vendor contamination:** no confirmed vendor mailbox survived as a recommended target, but broad page containers made contamination possible.
- **Repurposed domain:** confirmed for Artistic Civic Theatre; syntax and same-page evidence alone were insufficient to establish company association.
- **Page-heading contamination:** confirmed on Honeylove and Pops.
- **Wrong-person association:** confirmed as a general mechanism. A nearby or shared mailbox cannot safely be attributed to a person without stronger evidence.
- **Support/legal/accessibility semantics:** support and accessibility addresses were technically valid but poor partnership contacts and were overrepresented by a binary “found” measure.
- **Named-contact false negatives:** several first-party pages explicitly named founders, owners, managers, or staff, but the original extractor only reliably emitted records around addresses.
- **External-domain mailboxes:** some are legitimate (for example a Gmail store address or parent-company domain), but need an explicit validation label rather than being treated as ordinary same-domain addresses.

No testimonial, customer, guest-author, sponsor, or third-party widget identity was confirmed as a recommended contact in this sample.

## Targeted corrections

Only observed systematic issues were changed:

- page-heading/name filters reject FAQ, statement, committee, leadership, and similar labels;
- personal email association requires the email local part to agree with the nearby explicit first name;
- generic/shared local parts can never establish a named-person association;
- generic mailbox semantics recognize founder, partnership, growth, and business-development routes;
- support, accessibility, privacy, legal, billing, careers, DPO, and webmaster addresses receive a suitability penalty;
- external public domains are labeled `PUBLIC_EXTERNAL_DOMAIN` rather than ordinary syntax-valid same-domain addresses;
- homepage identity tokens must match the target company before contact evidence is accepted, catching the repurposed `actdalton.org` domain;
- stored evidence is revalidated during enrichment, so old cached false names do not survive stricter rules.

## After-results and interpretation

| Measure | Companies | Rate |
|---|---:|---:|
| Automated `CONTACT_FOUND` | 12 | 42.9% |
| Public email observed | 12 | 42.9% |
| Defensible named contact | 0 | 0.0% |
| Defensible named-person email association | 0 | 0.0% |
| Usable generic/unassociated mailbox | 9 | 32.1% |
| Incorrect named/company association | 0 | 0.0% |
| No reliable outreach contact | 19 | 67.9% |

The after-results are in `data/phase3_validation_after.csv`. Precision improved: the known wrong-company association and false people disappeared. Coverage fell, appropriately, because low-suitability accessibility/support mailboxes and ambiguous identities are no longer presented as strong contacts.

The system is safe enough to provide a **generic-mailbox research queue with mandatory human review**. It is **not yet reliable enough to claim named-contact discovery**. The correct operational interpretation of `CONTACT_FOUND` is “public contact evidence exists,” not “a suitable named outreach recipient exists.” `CONTACT_NOT_FOUND` remains a valid and common result.

## Recommendation

Do not begin outreach or Phase 4 from these results. Before named contacts are operationally useful, Phase 3 needs a separately validated, structure-aware person extractor that can represent a named person without an email and can preserve a shared mailbox as a distinct generic contact. That work should target first-party staff/team/about markup and be measured on this frozen sample without relaxing the false-association safeguards.
