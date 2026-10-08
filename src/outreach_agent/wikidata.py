"""Small, bounded Wikidata Query Service helpers used by Phase 4C evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import csv
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from outreach_agent.normalization import normalize_domain

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
SPARQL_QUERY = '''SELECT ?company ?website WHERE {
  ?company wdt:P31 wd:Q4830453;
           wdt:P856 ?website.
}
ORDER BY ?company
LIMIT 50'''
logger = logging.getLogger(__name__)


class SnapshotStore:
    """Atomic, no-overwrite store for successful raw HTTP JSON responses."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def paths(self, batch_id: str) -> tuple[Path, Path]:
        return self.directory / f"{batch_id}.json", self.directory / f"{batch_id}.meta.json"

    def has(self, batch_id: str) -> bool:
        payload, metadata = self.paths(batch_id)
        return payload.is_file() and metadata.is_file()

    def load(self, batch_id: str) -> tuple[bytes, dict[str, Any]]:
        payload_path, metadata_path = self.paths(batch_id)
        return payload_path.read_bytes(), json.loads(metadata_path.read_text(encoding="utf-8"))

    def save_success(self, batch_id: str, response: httpx.Response, *, params: dict[str, str], request_timestamp: str) -> Path:
        if not 200 <= response.status_code < 300:
            raise ValueError("only successful HTTP responses may be snapshotted")
        payload_path, metadata_path = self.paths(batch_id)
        metadata = {
            "batch_id": batch_id,
            "request_timestamp_utc": request_timestamp,
            "url": str(response.request.url),
            "params": params,
            "status_code": response.status_code,
            "response_headers": {key.lower(): value for key, value in response.headers.items() if key.lower() in {"content-type", "etag", "last-modified", "date", "content-length"}},
            "response_sha256": hashlib.sha256(response.content).hexdigest(),
        }
        self._atomic_create(payload_path, response.content)
        self._atomic_create(metadata_path, json.dumps(metadata, sort_keys=True, indent=2).encode("utf-8"))
        return payload_path

    @staticmethod
    def _atomic_create(path: Path, content: bytes) -> None:
        if path.exists():
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def request_json_snapshot(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str],
    store: SnapshotStore,
    batch_id: str,
    timeout: float = 40,
    max_retries: int = 2,
    sleep=time.sleep,
) -> tuple[bytes, dict[str, Any], int]:
    """Fetch at most three times; persist every 2xx body before JSON parsing."""
    payload_path, metadata_path = store.paths(batch_id)
    if store.has(batch_id):
        payload, metadata = store.load(batch_id)
        return payload, metadata, 0
    if payload_path.exists() or metadata_path.exists():
        # Incomplete snapshots are never considered successful or overwritten.
        raise RuntimeError(f"incomplete snapshot exists for {batch_id}; inspect before resuming")
    retries = 0
    max_retries = min(max(0, max_retries), 2)
    for attempt in range(max_retries + 1):
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            response = client.get(url, params=params, timeout=timeout)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt >= max_retries:
                logger.error("Wikidata request exhausted retries batch=%s error=%s", batch_id, exc)
                raise
            retries += 1
            sleep(min(2 ** attempt, 8))
            continue
        if 200 <= response.status_code < 300:
            store.save_success(batch_id, response, params=params, request_timestamp=timestamp)
            return response.content, store.load(batch_id)[1], retries
        if response.status_code not in {429, 502, 503} or attempt >= max_retries:
            logger.error("Wikidata request failed batch=%s status=%s retries=%s", batch_id, response.status_code, retries)
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = min(max(float(retry_after), 0), 30)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    delay = min(max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0), 30)
                except (TypeError, ValueError, OverflowError):
                    delay = min(2 ** attempt, 8)
        else:
            delay = min(2 ** attempt, 8)
        retries += 1
        sleep(delay)
    raise RuntimeError(f"Wikidata request exhausted retries for {batch_id}")


def build_sparql_query(after_qid: str | None, limit: int = 20) -> str:
    if not 10 <= limit <= 25:
        raise ValueError("batch size must be between 10 and 25")
    cursor = f'FILTER(STR(?company) > "http://www.wikidata.org/entity/{after_qid}")' if after_qid else ""
    return f'''SELECT ?company ?website WHERE {{
  ?company wdt:P31 wd:Q4830453;
           wdt:P856 ?website.
  {cursor}
}}
ORDER BY ?company ?website
LIMIT {limit}'''


def write_csv_atomic(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def parse_sparql_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and group WDQS rows by QID, retaining distinct website claims."""
    try:
        bindings = payload["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid Wikidata SPARQL results envelope") from exc
    if not isinstance(bindings, list):
        raise ValueError("Wikidata SPARQL bindings must be a list")
    grouped: dict[str, dict[str, Any]] = {}
    for row in bindings:
        try:
            uri = row["company"]["value"]
            website = row["website"]["value"]
            qid = uri.rsplit("/", 1)[-1]
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Wikidata row is missing a company or website") from exc
        if not qid.startswith("Q") or not qid[1:].isdigit():
            raise ValueError(f"invalid Wikidata entity URI: {uri}")
        record = grouped.setdefault(qid, {"qid": qid, "entity_url": f"https://www.wikidata.org/wiki/{qid}", "websites": []})
        if website not in record["websites"]:
            record["websites"].append(website)
    return [grouped[qid] for qid in sorted(grouped, key=lambda value: int(value[1:]))]


def normalize_official_websites(values: list[str]) -> list[tuple[str, str]]:
    """Return distinct (URL, normalized domain) pairs; reject non-web URLs."""
    accepted: dict[str, str] = {}
    for value in values:
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            domain = normalize_domain(value)
        except (ValueError, TypeError):
            continue
        accepted.setdefault(domain, value)
    return [(url, domain) for domain, url in sorted(accepted.items())]


def wikidata_claim_values(entity: dict[str, Any], property_id: str) -> list[Any]:
    """Extract simple Wikidata mainsnak values without inventing missing facts."""
    values: list[Any] = []
    for claim in entity.get("claims", {}).get(property_id, []):
        try:
            value = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        if isinstance(value, dict) and "id" in value:
            value = value["id"]
        elif isinstance(value, dict) and "amount" in value:
            value = value["amount"].lstrip("+")
        elif isinstance(value, dict) and "time" in value:
            value = value["time"]
        values.append(value)
    return values


def is_third_party_profile(url: str) -> bool:
    """Conservatively reject common social/profile/directory hosts as P856 sites."""
    try:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return True
    blocked = (
        "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "youtube.com",
        "wikipedia.org", "crunchbase.com", "linktr.ee", "tiktok.com", "threads.net", "medium.com",
        "substack.com", "about.me", "beacons.ai",
    )
    return any(host == domain or host.endswith("." + domain) for domain in blocked)


WEBSITE_DIAGNOSTIC_STATUSES = {
    "VERIFIED", "AMBIGUOUS_IDENTITY", "INSUFFICIENT_EVIDENCE",
    "FETCH_FAILED", "ACCESS_BLOCKED", "REJECTED_MISMATCH",
}


def diagnose_website_identity(
    company_name: str,
    website_url: str,
    original_status: str,
    *,
    http_status: str = "",
    final_url: str = "",
    page_title: str = "",
) -> str:
    """Conservatively refine saved website evidence; never promote ambiguity.

    This is a deterministic diagnostic layer over already-captured evidence,
    not a network verifier. Existing VERIFIED results survive except when a
    one-token label collides with a longer unrelated domain (e.g. Deep vs
    deep2001.com). Clear unrelated gambling redirects are rejected.
    """
    original_status = original_status.upper()
    if original_status not in {"VERIFIED", "AMBIGUOUS", *WEBSITE_DIAGNOSTIC_STATUSES}:
        if original_status in {"BLOCKED", "ACCESS_BLOCKED"}:
            return "ACCESS_BLOCKED"
        if original_status == "FETCH_FAILED":
            return "FETCH_FAILED"
        return "INSUFFICIENT_EVIDENCE"
    if original_status in {"BLOCKED", "ACCESS_BLOCKED"} or http_status in {"401", "403", "429"}:
        return "ACCESS_BLOCKED"
    if original_status == "FETCH_FAILED" or not http_status:
        return "FETCH_FAILED"

    title = (page_title or "").casefold()
    label = (company_name or "").strip()
    if original_status == "VERIFIED":
        tokens = [token.casefold() for token in re.findall(r"[\w]+", label) if len(token) > 1]
        try:
            host = (urlsplit(final_url or website_url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            host = ""
        host_label = host.split(".")[0]
        if len(tokens) == 1 and tokens[0] in title and host_label != tokens[0]:
            return "INSUFFICIENT_EVIDENCE"
        return "VERIFIED"

    if original_status == "REJECTED_MISMATCH":
        return "REJECTED_MISMATCH"
    # One-word Wikidata labels and QID placeholders do not establish identity.
    tokens = [token.casefold() for token in re.findall(r"[\w]+", label) if len(token) > 1]
    if not tokens or label.casefold().startswith("q") and label[1:].isdigit():
        return "INSUFFICIENT_EVIDENCE"
    if not title.strip() or title.strip() in {"home", "homepage", "welcome"}:
        return "INSUFFICIENT_EVIDENCE"
    # Captured redirect and title are strong evidence of an unrelated hijacked
    # domain, unlike a timeout or access block. Do not generalize this rule.
    gambling_terms = ("casino", "togel", "judi bola")
    if any(term in title for term in gambling_terms):
        try:
            source_host = (urlsplit(website_url).hostname or "").lower().removeprefix("www.")
            target_host = (urlsplit(final_url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            source_host = target_host = ""
        if source_host and target_host and source_host != target_host:
            return "REJECTED_MISMATCH"
    return "AMBIGUOUS_IDENTITY"


def fetch_json(client: httpx.Client, url: str, *, params: dict[str, str], timeout: float = 45) -> dict:
    """One request, one Retry-After-aware retry for transient rate/service errors."""
    for attempt in range(2):
        response = client.get(url, params=params, timeout=timeout)
        if response.status_code == 429 and attempt == 0:
            try:
                wait = min(float(response.headers.get("Retry-After", "5")), 30)
            except ValueError:
                wait = 5
            time.sleep(max(1, wait))
            continue
        if response.status_code in {502, 503, 504} and attempt == 0:
            time.sleep(2)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("Wikidata request exhausted bounded retry budget")
