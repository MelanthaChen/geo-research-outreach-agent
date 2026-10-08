from __future__ import annotations


INDUSTRIES = {
    "software": "SaaS / Software", "saas": "SaaS / Software", "technology": "SaaS / Software",
    "retail": "Retail", "shopping": "Retail", "ecommerce": "E-commerce", "e-commerce": "E-commerce",
    "health": "Healthcare", "medical": "Healthcare", "hospital": "Healthcare",
    "education": "Education", "school": "Education", "finance": "Finance", "bank": "Finance",
    "insurance": "Finance", "real estate": "Real Estate", "construction": "Real Estate",
    "travel": "Travel / Hospitality", "hotel": "Travel / Hospitality", "restaurant": "Food / Restaurant",
    "food": "Food / Restaurant", "professional": "Professional Services", "consult": "Professional Services",
    "consumer": "Consumer Services", "service": "Consumer Services",
}


def normalize_industry(value: str | None) -> str:
    if not value:
        return "Unknown"
    lowered = value.casefold()
    for token, category in INDUSTRIES.items():
        if token in lowered:
            return category
    return "Other"


def size_category(value: int | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= 10:
        return "MICRO"
    if value <= 50:
        return "SMALL"
    if value <= 250:
        return "MEDIUM"
    return "LARGE"
