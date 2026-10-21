# Phase 7 GEO platform handoff

## Existing platform and integration boundary

The sibling `geo-engine` repository is a separate property-scoped application. Its documented interfaces include property creation, synchronous website audits (`POST /api/v1/audit/run`), and experiment runs (`POST /api/v1/experiment-lab/run`). The audit and experiment paths are not connected by a recommendation-to-experiment lineage yet, and the platform uses its own property and experiment storage. This repository therefore exports a versioned, read-only JSONL handoff rather than writing into the other repository's database or calling its APIs implicitly.

The export contains only hotel properties with `verification_status=VERIFIED` and `geo_eligibility_status=ELIGIBLE`. Each record is `geo-company-profile/v1`, keyed by the stable OSM property candidate ID. It carries official domain and website evidence, hotel-specific fields, qualification reasons, page title/description/status/content hashes, source links and ODbL attribution, plus contact-channel URLs and their review state. It deliberately excludes email addresses. Website changes, outreach readiness, and human review are explicit fields; exporting a profile does not perform an audit, edit a site, approve outreach, or create a platform property.

Generate the stable handoff with:

```bash
PYTHONPATH=src uv run python scripts/phase7_hotels.py profiles
```

Outputs: `data/geo_engine_hotel_profiles.jsonl` for hotel properties and `data/geo_engine_startup_profiles.jsonl` for verified, eligible records with explicit YC source attribution. Generic OSM businesses are never labeled startups, and employee size remains unknown unless evidence exists. Import remains a researcher-controlled operation in the GEO platform. Before enabling a direct adapter, the two projects need an agreed authentication mechanism, property identity mapping, duplicate policy, and a platform-side import endpoint that preserves source lineage.

## Company intelligence and strategy evidence

Hotel profiles use a property-specific schema: category, address/location, chain signal, amenities, room evidence, guest segments, operational evidence status, website pages, contact channels, qualification evidence, and source provenance. OSM amenities and room tags remain source claims, not independently verified facts. Operating-signal status remains review-required.

The pilot's bounded crawler currently saves page URL, title, meta description, response status, visible-text length, structured-data types, and a content hash; it does not persist full page text or exact heading/paragraph excerpts. As a result, the available pilot evidence supports audit opportunities such as checking for FAQ coverage or structured data, but does not support accurate quoted before/after content edits. Do not generate personalized exact-copy recommendations until a permitted content snapshot with excerpt offsets and source URL is retained. The Word template at `templates/company_geo_analysis_report_template.docx` is a professor-review template, not a set of completed company reports.

No LLM provider/model is configured for repeatable question generation in this repository, and the sibling platform's research experiments require their own configured provider and run. No baseline/post LLM comparison or measured strategy effect is claimed. The platform already has experiment prompt/run storage and objective citation evaluation; a safe next integration is to import verified profiles, run a baseline experiment through the existing experiment UI, and attach the profile ID and evidence manifest to the researcher-maintained run record. Strategy changes should remain proposed variants until compared experimentally.

## Source and scope caveats

The hotel discovery source is OpenStreetMap data distributed by Geofabrik under ODbL 1.0 with attribution/share-alike obligations. Each OSM hotel feature is a property candidate and is tagged `BUSINESS;HOTEL` for candidate classification, not proof of current operation, a separate company, or independent ownership. Repeated chain properties are retained as distinct locations and retain property-level IDs. Hotel cohort membership is separate; SMB membership is `NO_BY_DEFAULT`. Website identity, GEO eligibility, contact discovery, human review, and outreach approval are separate states.

An offline exact-canonical-domain crosswalk is exported as `data/phase7_cohort_domain_overlap.csv`; property-level cohort labels are in `data/phase7_hotel_cohort_membership.csv` (`PYTHONPATH=src uv run python scripts/phase7_hotels.py cohort-export`). These are possible links only: chain domains commonly serve multiple properties, so domain matches remain pending identity review and are not merged or counted as confirmed cross-cohort company overlap. Existing hotel-tagged records in the general-business candidate database remain there; the SMB reconciliation excludes them from its 5,000 target count by default.

The SMB/SME research target is not limited to the YC-attributed profile export. That JSONL file is a legacy GEO-engine handoff cohort, not the target definition. General business candidates may qualify when company identity, operating website evidence, GEO eligibility, size evidence, and contactability are independently supported. Unknown size remains unknown. The separate hotel target is not counted toward the SMB target without authorization.
