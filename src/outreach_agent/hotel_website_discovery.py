"""Bounded official-website discovery from property-linked Wikidata claims."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

from outreach_agent.hotels import HOTEL_DB, HOTEL_NONFIRST_PARTY_HOSTS, ROOT
from outreach_agent.normalization import normalize_domain

API_URL = "https://www.wikidata.org/w/api.php"
DISCOVERY_ROOT = ROOT / "data/phase7_hotel_website_discovery"
PILOT_EXPORT = DISCOVERY_ROOT / "phase7_hotel_website_discovery_pilot.csv"
SNAPSHOT_DIR = DISCOVERY_ROOT / "snapshots"
FIELDS = (
    "candidate_id", "property_name", "location", "source_record_id", "osm_source_url",
    "wikidata_qid", "wikidata_entity_url", "wikidata_label", "name_similarity",
    "official_website", "canonical_domain", "discovery_status", "confidence",
    "reason", "source_license_or_permission", "discovered_at", "snapshot_path",
)


def _name_similarity(left: str, right: str) -> float:
    def clean(value: str) -> str:
        value = value.casefold().replace("&", " and ")
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())

    a, b = clean(left), clean(right)
    if not a or not b:
        return 0.0
    direct = SequenceMatcher(None, a, b).ratio()
    token_a, token_b = set(a.split()), set(b.split())
    overlap = len(token_a & token_b) / max(1, len(token_a | token_b))
    return round(max(direct, overlap), 4)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    temp.replace(path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in FIELDS} for row in rows)
    temp.replace(path)


def _read_missing_candidates(database: Path) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return {row["source_record_id"]: dict(row) for row in db.execute(
            """SELECT candidate_id,company_name,location,source_record_id,source_url,chain_status
               FROM candidates WHERE verification_status='MISSING_OFFICIAL_WEBSITE'
                 AND source_name LIKE '%US Northeast%'""")}


def _osm_wikidata_links(pbf: Path, candidate_rows: dict[str, dict[str, Any]]) -> dict[str, str]:
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("Install with `uv sync --extra openstreetmap` to read the local OSM PBF") from exc
    links: dict[str, str] = {}

    class Handler(osmium.SimpleHandler):
        def _add(self, obj: Any, kind: str) -> None:
            record_id = f"{kind}/{obj.id}"
            if record_id in candidate_rows:
                qid = (obj.tags.get("wikidata") or "").strip()
                if re.fullmatch(r"Q[1-9][0-9]*", qid):
                    links[record_id] = qid

        def node(self, obj: Any) -> None: self._add(obj, "node")
        def way(self, obj: Any) -> None: self._add(obj, "way")
        def relation(self, obj: Any) -> None: self._add(obj, "relation")

    Handler().apply_file(str(pbf), locations=False)
    return links


def _official_website(entity: dict[str, Any]) -> str:
    claims = entity.get("claims", {}).get("P856", [])
    for claim in claims:
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value.strip()
    return ""


def discover_hotel_websites(*, pbf: Path, database: Path = HOTEL_DB, limit: int = 25,
                            timeout_seconds: float = 20.0, apply: bool = False,
                            client: httpx.Client | None = None) -> dict[str, Any]:
    """Inspect a small deterministic set of exact OSM→Wikidata property links.

    One bounded Wikidata API request is made for up to ``limit`` unique QIDs.
    Only property-name-matched, first-party P856 URLs can be applied to the
    hotel DB, and applied candidates remain DISCOVERED_UNVERIFIED.
    """
    if not 1 <= limit <= 25:
        raise ValueError("Website-discovery pilot limit must be between 1 and 25")
    missing = _read_missing_candidates(database)
    links = _osm_wikidata_links(pbf, missing)
    selected: list[dict[str, Any]] = []
    seen_qids: set[str] = set()
    for record_id, qid in sorted(links.items(), key=lambda item: item[0]):
        if qid in seen_qids:
            continue
        selected.append({**missing[record_id], "wikidata_qid": qid})
        seen_qids.add(qid)
        if len(selected) == limit:
            break
    if not selected:
        _write_csv(PILOT_EXPORT, [])
        return {"selected": 0, "request_count": 0, "rows": [], "export": str(PILOT_EXPORT)}

    params = {"action": "wbgetentities", "ids": "|".join(row["wikidata_qid"] for row in selected),
              "props": "labels|claims", "languages": "en", "format": "json", "formatversion": "2"}
    own_client = client is None
    client = client or httpx.Client(follow_redirects=True, headers={
        "User-Agent": "geo-research-outreach-agent/1.0 (bounded hotel website discovery; research)"})
    timestamp = datetime.now(timezone.utc).isoformat()
    response = client.get(API_URL, params=params, timeout=timeout_seconds)
    if not response.is_success:
        retry_after = response.headers.get("Retry-After", "")
        digest = hashlib.sha256(response.content).hexdigest()
        snapshot_path = SNAPSHOT_DIR / f"failed-{digest}.json"
        _atomic_json(snapshot_path, {"source_url": str(response.url), "request_timestamp": timestamp,
            "query_parameters": params, "http_status": response.status_code,
            "retry_after": retry_after, "content_sha256": digest,
            "response_body_excerpt": response.content[:1_000_000].decode("utf-8", errors="replace"),
            "outcome": "SOURCE_ACCESS_BLOCKED; no retry attempted"})
        if own_client:
            client.close()
        failed_rows = [{
            "candidate_id": row["candidate_id"], "property_name": row["company_name"],
            "location": row.get("location") or "", "source_record_id": row["source_record_id"],
            "osm_source_url": row["source_url"], "wikidata_qid": row["wikidata_qid"],
            "wikidata_entity_url": f"https://www.wikidata.org/wiki/{row['wikidata_qid']}",
            "discovery_status": "SOURCE_ACCESS_BLOCKED", "confidence": "NONE",
            "reason": f"Wikidata entity API returned HTTP {response.status_code}; no retry attempted; Retry-After={retry_after or 'not supplied'}",
            "source_license_or_permission": "OpenStreetMap ODbL 1.0 property→QID link; Wikidata CC0 1.0 facts not retrieved",
            "discovered_at": timestamp, "snapshot_path": str(snapshot_path),
        } for row in selected]
        _write_csv(PILOT_EXPORT, failed_rows)
        return {"selected": len(selected), "request_count": 1, "source_access_blocked": True,
                "http_status": response.status_code, "retry_after": retry_after,
                "discovered_urls": 0, "verified_websites": 0, "applied_to_hotel_db": 0,
                "rows": failed_rows, "snapshot": str(snapshot_path), "export": str(PILOT_EXPORT)}
    body = response.json()
    raw_digest = hashlib.sha256(response.content).hexdigest()
    snapshot_path = SNAPSHOT_DIR / f"{raw_digest}.json"
    _atomic_json(snapshot_path, {"source_url": str(response.url), "request_timestamp": timestamp,
        "query_parameters": params, "http_status": response.status_code,
        "content_sha256": raw_digest, "license": "CC0 1.0 (Wikidata structured data)", "response": body})
    if own_client:
        client.close()

    entities = body.get("entities", {})
    results: list[dict[str, Any]] = []
    for row in selected:
        entity = entities.get(row["wikidata_qid"], {})
        label = entity.get("labels", {}).get("en", {}).get("value", "")
        website = _official_website(entity)
        similarity = _name_similarity(row["company_name"], label)
        domain = ""
        try:
            domain = normalize_domain(website) if website else ""
        except ValueError:
            website = ""
        third_party = any(domain == host or domain.endswith("." + host) for host in HOTEL_NONFIRST_PARTY_HOSTS)
        if not website:
            status, confidence, reason = "NO_OFFICIAL_WEBSITE_CLAIM", "NONE", "Exact OSM property QID has no P856 official-website claim"
        elif third_party:
            status, confidence, reason = "THIRD_PARTY_WEBSITE_REJECTED", "NONE", "P856 points to a known booking, directory, or third-party domain"
            website, domain = "", ""
        elif similarity < 0.72:
            status, confidence, reason = "NAME_MISMATCH_REVIEW", "LOW", "Wikidata label does not sufficiently match the OSM property name"
        else:
            status, confidence, reason = "OFFICIAL_URL_DISCOVERED_UNVERIFIED", "MEDIUM", "Exact OSM wikidata tag links the property to a Wikidata item whose P856 claim lists this URL; website identity still requires first-party verification"
        result = {
            "candidate_id": row["candidate_id"], "property_name": row["company_name"],
            "location": row.get("location") or "", "source_record_id": row["source_record_id"],
            "osm_source_url": row["source_url"], "wikidata_qid": row["wikidata_qid"],
            "wikidata_entity_url": f"https://www.wikidata.org/wiki/{row['wikidata_qid']}",
            "wikidata_label": label, "name_similarity": similarity, "official_website": website,
            "canonical_domain": domain, "discovery_status": status, "confidence": confidence,
            "reason": reason, "source_license_or_permission": "OpenStreetMap ODbL 1.0 for the property→QID link; Wikidata structured data CC0 1.0",
            "discovered_at": timestamp, "snapshot_path": str(snapshot_path),
        }
        results.append(result)
    _write_csv(PILOT_EXPORT, results)
    applied = 0
    if apply:
        with sqlite3.connect(database) as db:
            for row in results:
                if row["discovery_status"] != "OFFICIAL_URL_DISCOVERED_UNVERIFIED":
                    continue
                db.execute("""UPDATE candidates SET website=?,canonical_domain=?,verification_status='DISCOVERED_UNVERIFIED',
                    verification_reason=?,evidence_json=? WHERE candidate_id=? AND verification_status='MISSING_OFFICIAL_WEBSITE'""",
                    (row["official_website"], row["canonical_domain"], row["reason"], json.dumps({
                        "website_discovery": {"source": "Wikidata P856", "source_url": row["wikidata_entity_url"],
                            "snapshot_path": row["snapshot_path"], "confidence": row["confidence"],
                            "osm_property_source_url": row["osm_source_url"], "qid": row["wikidata_qid"]}}, sort_keys=True), row["candidate_id"]))
                if db.execute("SELECT changes()").fetchone()[0]:
                    db.execute("INSERT OR IGNORE INTO hotel_provenance VALUES(?,?,?,?,?,?,?,?)", (
                        row["candidate_id"], "Wikidata official website claim (P856)", row["wikidata_entity_url"],
                        row["wikidata_qid"], "CC0 1.0 structured data; exact OSM property link retained",
                        "https://www.wikidata.org/w/api.php", str(snapshot_path), row["discovered_at"]))
                    applied += 1
            db.commit()
    return {"selected": len(selected), "request_count": 1, "discovered_urls": sum(bool(r["official_website"]) for r in results),
            "name_matched_urls": sum(r["discovery_status"] == "OFFICIAL_URL_DISCOVERED_UNVERIFIED" for r in results),
            "applied_to_hotel_db": applied, "rows": results, "snapshot": str(snapshot_path), "export": str(PILOT_EXPORT)}
