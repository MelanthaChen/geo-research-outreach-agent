"""Exact owner-controlled staging fixtures; never use for production candidates."""

SYNTHETIC_CLASSIFICATION = 'SYNTHETIC_OWNER_CONTROLLED_TEST_DATA'

OWNER_TEST_FIXTURES = {
    'geoairesume-owner-test-v1': {
        'website': 'https://geo-ai-resume.vercel.app',
        'recipient': 'yuxuan.chen.1031@outlook.com',
    },
    'whisker-haven-owner-demo-20261023-v1': {
        'website': 'https://whisker-haven-demo.vercel.app/',
        'recipient': 'yuxaun.chen.1031@outlook.com',
    },
}


def synthetic_fixture_copy(record, personalization):
    """Validate only the synthetic recipient copy for one exact saved fixture."""
    if not isinstance(record, dict) or not isinstance(personalization, dict):
        return False
    fixture = OWNER_TEST_FIXTURES.get(record.get('id'))
    if not fixture or record.get('fixture') is not True or record.get('website') != fixture['website']:
        return False
    copy = personalization.get('recipient_copy')
    if not isinstance(copy, dict) or copy.get('data_classification') != SYNTHETIC_CLASSIFICATION:
        return False
    if copy.get('supported_observation') is not None:
        return False
    return all(isinstance(copy.get(field), str) and copy[field].strip()
               for field in ('business_summary', 'distinctive_detail', 'research_relevance'))
