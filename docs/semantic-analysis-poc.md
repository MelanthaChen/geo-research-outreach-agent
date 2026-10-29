# Structured semantic analysis proof of concept

## Scope and execution

This proof of concept uses a bounded, robots-aware read of Lagoon New York’s public site. It does not use Neon, write an Outreach record, create an invitation, contact the company, send email, or call a model provider. The local environment had no semantic provider/model configuration, so no LLM semantic analysis was performed. The deterministic baseline is retained as a baseline; the reviewed interpretation below is explicitly human-authored.

The reproducible fetch command is:

```sh
PYTHONPATH=src python3 scripts/semantic_poc.py \
  --name 'Lagoon New York' \
  --website https://www.lagoonny.com/ \
  --output artifacts/semantic-poc/lagoon.json
```

The run retrieved the homepage, About, Events, and Services pages. The bounded extractor produced 139 candidate facts. `artifacts/semantic-poc/lagoon.json` stores page metadata, the full fact inventory, and the complete existing deterministic output without copying every paragraph from the public site. `artifacts/semantic-poc/lagoon-human-analysis.json` stores exact short excerpts selected for review, their source URLs, fact-to-claim mappings, the complete analysis contract, and the separate human review.

Primary source pages: [About](https://www.lagoonny.com/about), [Services](https://www.lagoonny.com/services), and [Events](https://www.lagoonny.com/events).

## Contract and analysis boundary

`src/outreach_v2/semantic_analysis.py` defines a versioned contract for entity identity, products/services, audiences, capabilities, source facts, proposed claims, evidence mappings, claim reviews, limitations, observed relationships, GEO hypotheses, unresolved claims, and the abstention decision. Every identity/category assertion, proposed claim, relationship, and hypothesis carries evidence IDs. Each source fact carries an exact excerpt, URL, attribution, page title, section, and observation time.

`validate_contract()` checks shape, supported URLs, quote presence, known evidence IDs, claim mappings, relationship/hypothesis references, and whether final-copy claims have a `FULLY_SUPPORTED` review. These are mechanical provenance checks only. A human or semantic evaluator assigns `FULLY_SUPPORTED`, `PARTIALLY_SUPPORTED`, `UNSUPPORTED`, `CONTRADICTED`, or `NOT_VERIFIABLE_FROM_SAMPLE`; the structural validator does not infer those judgments from word overlap.

`ConstrainedSemanticAnalyzer` is an injectable provider boundary. It requires an explicit `authorized=True`, passes a constrained contract schema and evidence-only instructions, parses the returned object, and runs structural/provenance validation. No provider implementation, credential lookup, default model, or network call is configured. An approved provider/model/budget would still need to be supplied by an authorized caller. The local environment check found the supported provider key and semantic model variables unset.

`DeterministicBaseline` wraps the current analyzer’s description, strength, provenance IDs, and recommendation result. It labels claim support `NOT_VERIFIABLE_FROM_SAMPLE` because current lexical/provenance checks do not establish entailment. It therefore abstains from marking claims semantically verified. The proof-of-concept human review is kept in a separate artifact, rather than being presented as model output.

## Lagoon evidence and human review

The extraction selected four first-party pages. The home page supplied the About, Events, and Services links; the current keyword-based selector chose those three pages. The Events page contributed many event entries and artist biographies, which is a noise/selection limitation in the existing extractor.

The selected excerpts support three company claims under this analyst review:

- The About page identifies Lagoon New York as a concept store, gallery, and creation space. The short exact excerpt and source fact are `about_identity` in the reviewed contract.
- The About page says wearable art is available for sale alongside a complementary curation. Fact `about_offering` supports that offering mix; the claim does not assert that it is unique in the market.
- The Services page says Amalya and Logan offer custom design within their own styles and invites customers to choose a silhouette. Fact `services_custom_design` supports a specific capability. The excerpt does not support claims about actual tailoring outcomes beyond the selected-silhouette language, so those details are omitted from the claim.

I marked these claims `FULLY_SUPPORTED` as attributed summaries of what the company publishes. This is an assistant-authored analyst assessment, not an independent human review or independent verification of Lagoon’s claims.

The observed information relationship is that business identity and the offering mix appear on About, custom design appears on Services, and event information appears on Events. A possible GEO research hypothesis is that a system using only one page may omit details from the other pages. That is a testable hypothesis, not an observed system failure. No AI search results, citation behavior, user confusion, or performance measurements were collected. The decision is therefore `ABSTAIN_NO_OBSERVED_GEO_DEFICIENCY`: no website-change recommendation is justified by this sample.

## Comparison with the existing analyzer

The current analyzer also produced a concise business description and a custom-design sentence and returned `EVIDENCE_VALID=true` and `OUTREACH_REVIEW_READY=true`. Those fields were selected by sentence scores, keyword bonuses, a custom-design phrase transformation, and lexical-overlap validation. Its research-relevance sentence was assembled from page titles, including rendered title strings such as “lagoon - events — lagoon.” It returned no recommendation because its narrow repeated-link-label detector did not match Lagoon’s sampled links.

The new contract makes the evidence-to-claim chain explicit and separates company facts, claims, observed page relationships, hypotheses, and the recommendation decision. The human review found the factual summaries defensible while refusing to turn page separation into a defect. The improvement is in inspectability and honest abstention; semantic summarization has **not** been automated or evaluated against an authorized model.

## Integration plan

1. Keep fetching, visible-content extraction, canonical URLs, source IDs, deduplication, and persistence deterministic. Expand extraction to retain semantic blocks and list content that the current paragraph-only profile misses; store raw page snapshots under the existing retention policy.
2. Build evidence profiles before any summarization. Add entity attribution and source roles, and rank evidence by directness, authority, specificity, recency, and relevance. Do not treat missing terms in the bounded sample as proof of absence.
3. When an owner authorizes a provider, send only the selected evidence profile under a request-scoped model, token, and cost ceiling. Require schema-constrained JSON with claim-level evidence IDs and explicit uncertainty. Do not enable model access by environment accident.
4. Run deterministic schema/provenance checks, then claim-evidence review. Treat the model’s support labels as proposed judgments; retain human review for final factual claims and any GEO recommendation until evaluations show reliable performance.
5. Evaluate blind cases across retail, professional services, hospitality, product businesses, and small websites. Measure unsupported-claim rate, false recommendations, useful abstentions, and reviewer agreement. Do not use length, lexical overlap, or pass rate as the success metric.
6. Only then adapt the contract into persisted personalization and invitation preview. Preserve existing invitation, consent, and research records, and keep generation separate from sending.

## Verification

The focused contract and personalization regression suite is run with:

```sh
PYTHONPATH=src pytest -q tests/rebuild/test_semantic_analysis_contract.py \
  tests/rebuild/test_personalization_quality.py --tb=short
```

The suite checks URL/evidence provenance, mappings, unreviewed final claims, explicit abstention, all five review states, and the no-call authorization boundary. It does not claim that a model generated semantically correct content.
