# US geographic eligibility audit

Audit date: 2026-10-22

## Scope and method

The audit read `data/phase7_candidates.db`, `data/hotel_candidates.db`, and the lifecycle read model `data/research_registry.db`. The source databases were opened with SQLite read-only connections. No source rows, crawler checkpoints, or hotel-worker settings were changed.

Geographic classification is stored separately in the lifecycle registry as `geographic_classification` and `geographic_evidence_json`:

- `VERIFIED_US`: explicit US state/country evidence, a recognized US directory location, or reliable US Geofabrik physical-source provenance;
- `VERIFIED_NON_US`: explicit non-US country evidence;
- `COUNTRY_UNKNOWN`: no reliable country evidence;
- `COUNTRY_CONFLICT`: explicit US and non-US evidence conflict.

Domains, language, `.com` domains, and generic international service availability are not used as US evidence. Hotel classification uses the physical property location/source extract; parent chain headquarters are ignored.

## Candidate distributions

The source counts were 57,664 company candidates and 15,262 hotel candidates. Current registry classifications are:

| Cohort | VERIFIED_US | VERIFIED_NON_US | COUNTRY_UNKNOWN | COUNTRY_CONFLICT | Total |
|---|---:|---:|---:|---:|---:|
| Companies | 57,574 | 84 | 6 | 0 | 57,664 |
| Hotels | 15,262 | 0 | 0 | 0 | 15,262 |

Company US evidence is dominated by the OpenStreetMap Geofabrik US Northeast extract, with explicit US directory locations used for the remaining US directory records. Non-US companies are primarily the Y Combinator records with explicit foreign locations. Hotel candidates came from California, Georgia, Texas, and US Northeast physical extracts.

The previously reported “13,482 verified records with contact evidence” is not stable because the independently running hotel worker changed the source contact-channel rows during the audit window. The current read-only snapshot contains 13,586 records with both `identity_verification_status=VERIFIED` and contact evidence: 13,121 companies and 465 hotels. Their geographic distribution is:

| Verified/contact-evidence subset | VERIFIED_US | VERIFIED_NON_US | COUNTRY_UNKNOWN | COUNTRY_CONFLICT | Total |
|---|---:|---:|---:|---:|---:|
| Current snapshot | 13,556 | 28 | 2 | 0 | 13,586 |

For the required US-target counts, the enforceable current totals are:

- US-verified contactable companies: **13,091**;
- US-verified contactable hotels: **465**.

The current US-verified, identity-verified, qualification-eligible research-target counts are **18,942 companies** and **879 hotels**. These are the only company/hotel records exposed as eligible GEO target counts by the registry-backed dashboard summary.

“Contactable” means saved non-rejected public contact evidence exists; it does not mean human outreach approval.

## Eligibility enforcement

`VERIFIED_US` is now mandatory for:

- preliminary GEO screening;
- questionnaire invitations and recruitment email drafts;
- campaign selection and mail-merge previews;
- GEO handoff readiness;
- geographic target counts and dashboard filtering.

Foreign, unknown, and conflicting records remain in the registry with their original source IDs and provenance, but fail closed at those gates. GeoAIResume remains an `OWNER_CONTROLLED_TEST` record and is excluded from formal company/hotel counts; its explicit local fixture workflow remains available only under the existing local-test protocol.

## Dashboard

The Research Queue now provides a geographic filter with:

- US-verified;
- non-US;
- country unknown;
- country conflict.

The filtered API and CSV export include `geographic_classification`. Existing record detail, cohort separation, source evidence, and lifecycle state remain intact.

## Tests

Added coverage for:

- international hotel chains classified by physical property location;
- multinational companies with explicit US operating locations;
- ambiguous and missing addresses;
- US/non-US evidence conflicts;
- false US matches from domains/language and street names;
- non-US screening and email-draft blocking;
- geographic queue filtering.

Full suite result:

```bash
PYTHONPATH=src uv run pytest -q
# 279 passed
```

No emails were sent, no forms submitted, no paid LLM calls or deployments were performed, and no commit or push was made.
