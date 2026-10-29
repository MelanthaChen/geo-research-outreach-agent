from outreach_v2.quality import quality_decisions, quality_reasons


def personalization():
    about = "https://lagoon.example/about"
    facts = [
        {"id": "about", "statement": "Lagoon is a Brooklyn concept store, gallery, and creation space.",
         "evidence_excerpt": "Imagined by its founders, Lagoon is a concept store, gallery, and creation space based in Brooklyn.",
         "source_content": "About Lagoon. Imagined by its founders, Lagoon is a concept store, gallery, and creation space based in Brooklyn.",
         "source_urls": [about], "attribution": "COMPANY_STATED", "classification": "TARGET_BUSINESS",
         "assertion_type": "ATTRIBUTED_COMPANY_DESCRIPTION"},
        {"id": "custom", "statement": "Custom designs can be tailored to a selected silhouette, size, fabric, and color.",
         "evidence_excerpt": "Our custom design service lets you select a silhouette and tailor the size, fabric, and colors.",
         "source_content": "Services. Our custom design service lets you select a silhouette and tailor the size, fabric, and colors.",
         "source_urls": [about], "attribution": "COMPANY_STATED", "classification": "PRODUCT_OR_SERVICE",
         "assertion_type": "FIRST_PARTY_PAGE_CLAIM"},
    ]
    description = "Lagoon New York is a Brooklyn-based concept store that brings together retail, gallery, and creation-space functions."
    strength = "Its custom-design service lets clients choose a silhouette and tailor the garment's size, fabric, and colors."
    relevance = "The public site connects the business identity with a tailored service, giving researchers a concrete case for studying how AI systems represent offerings."
    return {
        "analysis_version": "structured-evidence-v2",
        "business_description": description,
        "company_strength": strength,
        "source_urls": [about],
        "recommendations": [],
        "assessment": {"geo_observation": "NOT_ESTABLISHED", "human_review_required": True},
        "claim_support": {"business_description": ["about"], "company_strength": ["custom"]},
        "evidence_profile": {
            "pages": [{"url": about}],
            "facts": facts,
        },
        "recipient_copy": {
            "business_summary": description,
            "distinctive_detail": strength,
            "research_relevance": relevance,
            "support": {"business_summary": ["about"], "distinctive_detail": ["custom"], "research_relevance": ["about", "custom"]},
        },
    }


def test_supported_paraphrased_summaries_and_explicit_geo_abstention_are_review_ready():
    p = personalization()
    decisions = quality_decisions(p)
    assert quality_reasons(p) == []
    assert decisions["EVIDENCE_VALID"]["passed"]
    assert decisions["OUTREACH_REVIEW_READY"]["passed"]
    assert decisions["CLAIM_GROUNDING"]["status"] == "AUTOMATED_FILTERING"
    assert p["recommendations"] == []
    assert p["assessment"]["geo_observation"] == "NOT_ESTABLISHED"


def test_invalid_recipient_summary_evidence_mapping_still_fails():
    p = personalization()
    p["recipient_copy"]["support"]["distinctive_detail"] = ["missing-fact"]
    decisions = quality_decisions(p)
    assert decisions["EVIDENCE_VALID"]["passed"]
    assert not decisions["OUTREACH_REVIEW_READY"]["passed"]
    assert "UNSUPPORTED_RECIPIENT_SUMMARY" in decisions["OUTREACH_REVIEW_READY"]["reasons"]



def test_recipient_research_relevance_requires_traceable_source_facts():
    p = personalization()
    p["recipient_copy"]["support"].pop("research_relevance")
    decisions = quality_decisions(p)
    assert not decisions["OUTREACH_REVIEW_READY"]["passed"]
    assert "UNSUPPORTED_RECIPIENT_SUMMARY" in decisions["OUTREACH_REVIEW_READY"]["reasons"]
