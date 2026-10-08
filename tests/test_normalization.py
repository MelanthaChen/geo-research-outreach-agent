import pytest

from outreach_agent.normalization import normalize_company_name, normalize_domain, normalize_url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com", "example.com"),
        ("https://www.example.com/", "example.com"),
        ("http://example.com/about", "example.com"),
        ("EXAMPLE.COM", "example.com"),
    ],
)
def test_domain_normalization(value, expected):
    assert normalize_domain(value) == expected


def test_url_normalization():
    assert normalize_url("http://www.Example.com/about/") == "https://example.com/about"


def test_company_name_normalization():
    assert normalize_company_name("  Acme, Inc. ") == "acme"
    assert normalize_company_name("ACME Corporation") == "acme"

