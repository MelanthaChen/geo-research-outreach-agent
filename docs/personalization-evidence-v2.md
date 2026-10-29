# Personalization evidence rules (v2)

The analyzer treats factual personalization and GEO observations as separate outputs.

## Personalization evidence is sufficient when

- At least one permitted, same-site first-party page contains a readable, specific business statement.
- The description and strength each quote or closely reproduce a source excerpt and retain its exact URL.
- The strength describes an attributable company statement about a real offering, expertise, product, service, process, or other specific distinction. It is presented as what the company says, not as independently proven fact.
- The quality validator finds no code contamination, generic filler, missing provenance, or unsupported claim.

One detailed authoritative first-party page can satisfy these conditions. Other pages may corroborate or add context; there is no mandatory page count. A blocked or failed secondary page is recorded as a limitation and is never bypassed. Its failure alone does not invalidate usable accessible evidence.

## A GEO observation is supported when

The inspected content directly supports a bounded statement about a specific page element, relationship, or published fact, and the statement cites the relevant excerpt, exact same-site URL, and `observed_at` timestamp. The recommendation must address that observed item with a concrete action. Missing keywords or material in the sampled pages do not prove that the whole site lacks it. The analyzer currently emits no GEO finding unless such support is available; it does not invent one to fill a template.

When no defensible observation is established, v2 records `assessment.geo_observation = NOT_ESTABLISHED` and an empty `recommendations` list. The template renders an explicit limitation sentence in that case. A generic recommendation is never substituted.

## Invitation and review states

- `personalization_evidence = SUFFICIENT` means the attributed business description and strength passed evidence and quality validation.
- `geo_observation = SUPPORTED` requires at least one separately validated observation; `NOT_ESTABLISHED` means none was established from the permitted sample.
- `invitation = ELIGIBLE_FOR_REVIEW` means the factual profile may be used in a draft invitation. It does not imply identity or consent authorization, send authorization, or research authorization.
- `human_review_required = true` remains required for review of the complete invitation and its evidence before any use. This status does not add a second research-approval step or authorize sending.

The current deterministic analyzer is extractive and conservative. It can preserve and attribute first-party claims, but it does not independently verify those claims or perform broad AI-search measurements. Those limitations are included in the evidence profile and invitation wording.
