from outreach_agent.geographic_eligibility import (
    COUNTRY_CONFLICT,
    COUNTRY_UNKNOWN,
    VERIFIED_NON_US,
    VERIFIED_US,
    classify_geography,
)


def test_international_hotel_chain_uses_property_location():
    result, evidence = classify_geography(
        location="Paris, France", source_name="OpenStreetMap via Geofabrik France extract",
        source_batch="france-latest.osm.pbf", profile_type="hotel_property",
    )
    assert result == VERIFIED_NON_US
    assert evidence["physical_property_rule"] is True


def test_multinational_company_with_us_entity_is_us_when_location_is_explicit():
    result, _ = classify_geography(location="New York, NY", source_name="Directory", profile_type="business_company")
    assert result == VERIFIED_US


def test_ambiguous_address_is_unknown():
    result, _ = classify_geography(location="Manchester", source_name="Directory")
    assert result == COUNTRY_UNKNOWN


def test_missing_country_data_is_unknown_without_source_provenance():
    result, _ = classify_geography(location="", source_name="Directory")
    assert result == COUNTRY_UNKNOWN


def test_conflicting_us_and_non_us_evidence_is_not_eligible():
    result, _ = classify_geography(location="Toronto, Canada", address="Austin, TX")
    assert result == COUNTRY_CONFLICT


def test_us_extract_is_not_a_false_match_from_domain_or_language():
    result, evidence = classify_geography(location="", source_name="OpenStreetMap via Geofabrik Texas extract", source_batch="texas-latest.osm.pbf", profile_type="hotel_property")
    assert result == VERIFIED_US
    assert evidence["rule"] == "Geofabrik US physical-source provenance"

    result, _ = classify_geography(location="London, United Kingdom", source_name="Directory")
    assert result == VERIFIED_NON_US
