from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


def normalize_company_name(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    suffixes = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "company", "co"}
    parts = value.split()
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)


def _parsed_url(value: str):
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("website cannot be empty")
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    parsed = urlsplit(cleaned)
    if not parsed.hostname:
        raise ValueError(f"invalid website URL: {value}")
    return parsed


def normalize_domain(value: str) -> str:
    host = (_parsed_url(value).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid domain: {host}") from exc


def normalize_url(value: str) -> str:
    parsed = _parsed_url(value)
    domain = normalize_domain(value)
    port = parsed.port
    netloc = domain if port is None or port in {80, 443} else f"{domain}:{port}"
    path = parsed.path.rstrip("/") or ""
    return urlunsplit(("https", netloc, path, parsed.query, ""))

