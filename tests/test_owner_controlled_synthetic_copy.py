import pytest

from outreach_v2.owner_test_fixtures import (
    OWNER_TEST_FIXTURES, SYNTHETIC_CLASSIFICATION, synthetic_fixture_copy,
)
from outreach_v2.template import render, validate


CASES = [
    (
        'geoairesume-owner-test-v1',
        'GeoAIResume controlled test website',
        'https://geo-ai-resume.vercel.app',
        'GeoAIResume is a test website demonstrating how an AI-assisted resume platform can present its features, workflows, and product information to visitors.',
        'Its test scenario focuses on helping users explore resume creation, content refinement, and the presentation of professional experience through an AI-assisted workflow.',
        "This website provides a controlled example for studying how AI-powered search systems interpret a digital product's purpose, features, and intended audience.",
    ),
    (
        'whisker-haven-owner-demo-20261023-v1',
        'Whisker Haven (Fictional Demo)',
        'https://whisker-haven-demo.vercel.app/',
        'Whisker Haven is a fictional pet-care business used as a controlled website and email test fixture for the GEO research outreach workflow.',
        'Its simulated business profile centers on pet-care services, everyday animal well-being, and accessible information for pet owners.',
        'Whisker Haven provides a controlled local-service example for examining how AI-powered search connects business descriptions, service categories, and customer questions.',
    ),
]


@pytest.mark.parametrize('ident,name,website,summary,detail,relevance', CASES)
def test_exact_owner_fixture_accepts_and_renders_only_classified_synthetic_copy(
    ident, name, website, summary, detail, relevance
):
    record = {'id': ident, 'name': name, 'website': website, 'fixture': True}
    personalization = {
        'data_classification': 'OBSERVED',  # Existing observed evidence remains distinct.
        'recipient_copy': {
            'business_summary': summary,
            'distinctive_detail': detail,
            'research_relevance': relevance,
            'supported_observation': None,
            'data_classification': SYNTHETIC_CLASSIFICATION,
        },
    }

    assert synthetic_fixture_copy(record, personalization)
    assert validate(personalization, website, record=record) is personalization
    subject, body = render(name, website, personalization, 'https://forms.example/test', 'PREVIEW',
        record=record, template_config={
            'researcher_name': 'Researcher', 'university_affiliation': 'University',
            'reply_to': 'reply@example.edu', 'sender': 'sender@example.edu',
            'sender_name': 'Researcher',
        })
    assert name in subject
    for text in (summary, detail, relevance):
        assert text in body
    assert 'SYNTHETIC PERSONALIZATION - NOT VERIFIED WEBSITE EVIDENCE' in body
    assert 'No website observation is asserted' in body
    assert 'One detail that stood out' not in body


def test_synthetic_copy_does_not_expand_to_other_candidate_or_claim_observation():
    ident, name, website, summary, detail, relevance = CASES[0]
    record = {'id': ident, 'name': name, 'website': website, 'fixture': True}
    personalization = {
        'recipient_copy': {
            'business_summary': summary, 'distinctive_detail': detail,
            'research_relevance': relevance, 'supported_observation': None,
            'data_classification': SYNTHETIC_CLASSIFICATION,
        },
    }
    assert not synthetic_fixture_copy({**record, 'id': 'unrelated-candidate'}, personalization)
    assert not synthetic_fixture_copy({**record, 'website': 'https://other.example'}, personalization)
    assert not synthetic_fixture_copy({**record, 'fixture': False}, personalization)
    assert not synthetic_fixture_copy(record, {
        **personalization,
        'recipient_copy': {**personalization['recipient_copy'], 'supported_observation': 'Invented detail'},
    })
    assert OWNER_TEST_FIXTURES[ident]['recipient'] == 'yuxuan.chen.1031@outlook.com'
    with pytest.raises(ValueError):
        validate(personalization, website, record={'id': 'unrelated-candidate', 'fixture': True, 'website': website})
