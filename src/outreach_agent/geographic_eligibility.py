"""Conservative geographic eligibility classification for research targets.

This module classifies saved source facts without changing the source databases.
It deliberately does not use domains, language, or generic service availability
as evidence of a US company or property.
"""

from __future__ import annotations

import re
from typing import Any

VERIFIED_US = "VERIFIED_US"
VERIFIED_NON_US = "VERIFIED_NON_US"
COUNTRY_UNKNOWN = "COUNTRY_UNKNOWN"
COUNTRY_CONFLICT = "COUNTRY_CONFLICT"

US_STATE_CODES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
US_COUNTRY_TERMS = ("US", "USA", "U.S.", "UNITED STATES", "UNITED STATES OF AMERICA")
US_CITY_NAMES = {
    "albany", "allentown", "anaheim", "atlanta", "austin", "boston", "brooklyn",
    "buffalo", "cambridge", "dallas", "el paso", "fort worth", "fresno", "harrisburg",
    "houston", "irving", "lancaster", "los angeles", "new york", "new york city",
    "philadelphia", "pittsburgh", "portland", "rochester", "sacramento", "san antonio",
    "san diego", "san francisco", "savannah", "state college", "syracuse",
}
NON_US_TERMS = {
    "argentina", "australia", "austria", "belgium", "brazil", "canada", "chile", "china",
    "colombia", "costa rica", "croatia", "denmark", "finland", "france", "germany",
    "hong kong", "india", "indonesia", "ireland", "israel", "italy", "japan", "jordan",
    "kenya", "lithuania", "malaysia", "mexico", "netherlands", "new zealand", "nigeria",
    "norway", "pakistan", "poland", "portugal", "romania", "singapore", "slovenia",
    "south africa", "south korea", "spain", "sweden", "switzerland", "taiwan", "thailand",
    "turkey", "united arab emirates", "united kingdom", "vietnam",
}


def _state_evidence(location: str, address: str) -> str | None:
    for part in re.split(r"[,;/|]", f"{location},{address}"):
        token = part.strip().upper().replace(".", "")
        if token in US_STATE_CODES:
            return token
    return None


def classify_geography(*, location: str | None, address: str | None = None,
                       source_name: str | None = None, source_batch: str | None = None,
                       profile_type: str = "business_company") -> tuple[str, dict[str, Any]]:
    location_text = (location or "").strip()
    address_text = (address or "").strip()
    source_text = f"{source_name or ''} {source_batch or ''}".strip()
    normalized_location = location_text.casefold()
    normalized = f"{location_text} {address_text}".casefold()
    non_us = sorted(term for term in NON_US_TERMS if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", normalized_location))
    state = _state_evidence(location_text, address_text)
    explicit_us = any(re.search(rf"(?<![a-z]){re.escape(term.casefold())}(?![a-z])", normalized) for term in US_COUNTRY_TERMS)
    city = location_text.split(",", 1)[0].strip().casefold() in US_CITY_NAMES
    us_extract = "geofabrik" in source_text.casefold() and (
        "us" in source_text.casefold() or any(state_name in source_text.casefold() for state_name in ("california", "texas", "georgia"))
    )

    if us_extract:
        classification = VERIFIED_US
        rule = "Geofabrik US physical-source provenance"
    elif non_us and (state or explicit_us or us_extract):
        classification = COUNTRY_CONFLICT
        rule = "US evidence and non-US country text conflict"
    elif non_us:
        classification = VERIFIED_NON_US
        rule = "explicit non-US country in saved location/address"
    elif us_extract:
        classification = VERIFIED_US
        rule = "Geofabrik US extract is physical-source provenance"
    elif state or explicit_us:
        classification = VERIFIED_US
        rule = "explicit US state or country in saved location/address"
    elif city:
        classification = VERIFIED_US
        rule = "recognized US city in authoritative directory location"
    else:
        classification = COUNTRY_UNKNOWN
        rule = "no reliable country evidence in saved location/address/provenance"

    return classification, {
        "rule": rule,
        "location": location_text,
        "address": address_text,
        "source_name": source_name or "",
        "source_batch": source_batch or "",
        "source_provenance_used": source_text,
        "profile_type": profile_type,
        "non_us_terms": non_us,
        "state_evidence": state,
        "physical_property_rule": profile_type == "hotel_property",
    }


def is_us_eligible(classification: str) -> bool:
    return classification == VERIFIED_US
