"""Bounded, resumable Wikidata retrieval and offline pilot export."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import httpx

from outreach_agent.config import WebsiteSettings, load_yaml
from outreach_agent.models import Company
from outreach_agent.normalization import normalize_company_name
from outreach_agent.qualification import evaluate
from outreach_agent.website import WebsiteCollector
from outreach_agent.wikidata import (
    SPARQL_ENDPOINT,
    SnapshotStore,
    build_sparql_query,
    is_third_party_profile,
    normalize_official_websites,
    parse_sparql_results,
    request_json_snapshot,
    wikidata_claim_values,
    write_csv_atomic,
)

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "data/phase4c1_wikidata_snapshots"
UA = "GEOResearchOutreachAgent/0.5 (+https://github.com/MelanthaChen/geo-research-outreach-agent; academic research; no outreach)"
LOG = logging.getLogger("phase4c1")


def _entity_batch(client: httpx.Client, store: SnapshotStore, qids: list[str], *, offline: bool) -> dict:
    if not qids:
        return {}
    groups = [qids[index:index + 50] for index in range(0, len(qids), 50)]
    result = {}
    for index, group in enumerate(groups, 1):
        digest = hashlib.sha256("|".join(group).encode("utf-8")).hexdigest()[:16]
        batch_id = f"entities-{index:03d}-{digest}"
        if offline and not store.has(batch_id):
            continue
        params = {"action": "wbgetentities", "ids": "|".join(group), "props": "labels|claims", "languages": "en", "format": "json", "formatversion": "2"}
        body, _, _ = request_json_snapshot(client, "https://www.wikidata.org/w/api.php", params=params, store=store, batch_id=batch_id, timeout=20, max_retries=2)
        result.update(json.loads(body).get("entities", {}))
    return result


def _all_records(store: SnapshotStore) -> tuple[list[dict], int]:
    records_by_qid: dict[str, dict] = {}
    snapshot_paths = [path for path in store.directory.glob("wdqs-batch-*.json") if not path.name.endswith(".meta.json")]
    for path in sorted(snapshot_paths):
        batch_id = path.stem
        payload = json.loads(path.read_bytes())
        for record in parse_sparql_results(payload):
            target = records_by_qid.setdefault(record["qid"], {**record, "websites": []})
            for website in record["websites"]:
                if website not in target["websites"]:
                    target["websites"].append(website)
    return sorted(records_by_qid.values(), key=lambda row: row["entity_url"]), len(snapshot_paths)


def _fetch_batches(client: httpx.Client, store: SnapshotStore, target: int, batch_size: int, offline: bool) -> tuple[int, int]:
    retries = 0
    failures = 0
    for _ in range(8):
        records, _ = _all_records(store)
        if len(records) >= target or offline:
            break
        after_qid = records[-1]["qid"] if records else None
        batch_id = f"wdqs-batch-after-{after_qid or 'START'}-limit-{batch_size}"
        if store.has(batch_id):
            # The successful page is reused; proceed from its largest QID next pass.
            continue
        query = build_sparql_query(after_qid, batch_size)
        params = {"query": query, "format": "json"}
        try:
            _, _, used_retries = request_json_snapshot(client, SPARQL_ENDPOINT, params=params, store=store, batch_id=batch_id, timeout=40, max_retries=2)
            retries += used_retries
            refreshed, _ = _all_records(store)
            if len(refreshed) <= len(records):
                break
            # Wait between public WDQS requests; avoid concurrent/burst traffic.
            time.sleep(1.0)
        except (httpx.HTTPError, RuntimeError) as exc:
            LOG.error("batch failed batch=%s error=%s", batch_id, exc)
            failures += 1
            break
    return retries, failures


def _label(entity: dict, qid: str) -> str:
    return entity.get("labels", {}).get("en", {}).get("value", qid)


def _claims(entity: dict, prop: str) -> str:
    return " | ".join(str(value) for value in wikidata_claim_values(entity, prop))


def _employee_numbers(value: str) -> list[float]:
    numbers = []
    for item in value.split(" | "):
        match = re.match(r"\s*(\d+(?:\.\d+)?)", item)
        if match:
            numbers.append(float(match.group(1)))
    return numbers


def _verify_site(collector: WebsiteCollector, qid: str, name: str, url: str, company_id: int) -> dict[str, str]:
    if is_third_party_profile(url):
        return {"qid": qid, "company_name": name, "website_url": url, "status": "REJECTED_THIRD_PARTY", "http_status": "", "final_url": "", "page_title": "", "website_signals": "{}", "evidence": "P856 points to a common third-party profile host"}
    company = Company(id=company_id, company_name=name, website=url)
    try:
        snapshot = collector.inspect(company)
    except Exception as exc:  # a site-level error must not abort the bounded cohort
        return {"qid": qid, "company_name": name, "website_url": url, "status": "FETCH_FAILED", "http_status": "", "final_url": "", "page_title": "", "website_signals": "{}", "evidence": type(exc).__name__}
    title = snapshot.title or ""
    final_url = snapshot.final_url or ""
    status = "FETCH_FAILED"
    evidence = snapshot.error_message or snapshot.fetch_status
    signals = snapshot.extracted_signals or "{}"
    if snapshot.fetch_status == "ROBOTS_DENIED":
        status = "ROBOTS_DENIED"
    elif snapshot.fetch_status == "BLOCKED":
        status = "BLOCKED"
    elif snapshot.fetch_status in {"SUCCESS", "PARTIAL"}:
        if final_url and is_third_party_profile(final_url):
            return {"qid": qid, "company_name": name, "website_url": url, "status": "REJECTED_THIRD_PARTY", "http_status": str(snapshot.http_status or ""), "final_url": final_url, "page_title": title, "website_signals": signals, "evidence": "company website redirected to a common third-party profile host"}
        title_key = normalize_company_name(title)
        name_key = normalize_company_name(name)
        substantive = [part for part in name_key.split() if len(part) > 2]
        if name_key and (name_key in title_key or (substantive and all(part in title_key for part in substantive))):
            status = "VERIFIED"
            evidence = "reachable first-party homepage title matches company name"
        else:
            status = "AMBIGUOUS"
            evidence = "homepage reachable, but title did not confirm the Wikidata company identity"
        parked = ("domain for sale", "buy this domain", "parked domain", "domain parking")
        if any(term in (title + " " + (snapshot.meta_description or "")).lower() for term in parked):
            status = "REJECTED_PARKED"
            evidence = "page title or description indicates a parked/for-sale domain"
    return {"qid": qid, "company_name": name, "website_url": url, "status": status, "http_status": str(snapshot.http_status or ""), "final_url": final_url, "page_title": title, "website_signals": signals, "evidence": evidence}


def export_pilot(*, store: SnapshotStore, target: int, retries: int, failures: int, offline: bool) -> dict[str, int]:
    records, batch_count = _all_records(store)
    raw_binding_rows = 0
    for path in store.directory.glob("wdqs-batch-*.json"):
        if not path.name.endswith(".meta.json"):
            raw_binding_rows += len(json.loads(path.read_bytes()).get("results", {}).get("bindings", []))
    qids = [record["qid"] for record in records[:target]]
    with httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, follow_redirects=True) as client:
        try:
            entities = _entity_batch(client, store, qids, offline=offline)
        except (httpx.HTTPError, RuntimeError) as exc:
            LOG.error("Wikidata entity metadata unavailable; preserving query records: %s", exc)
            entities = {}
    # Select one deterministic first-party candidate per QID; keep duplicate-domain rows but flag them.
    candidates = []
    for record in records[:target]:
        entity = entities.get(record["qid"], {})
        name = _label(entity, record["qid"])
        sites = normalize_official_websites(record["websites"])
        if not sites:
            continue
        website, domain = sites[0]
        candidates.append({"qid": record["qid"], "company_name": name, "website_url": website, "normalized_domain": domain,
                           "entity_url": record["entity_url"], "industry_qids": _claims(entity, "P452"),
                           "country_qids": _claims(entity, "P17"), "admin_qids": _claims(entity, "P131"),
                           "headquarters_qids": _claims(entity, "P159"), "employee_values": _claims(entity, "P1128"),
                           "inception_values": _claims(entity, "P571"), "dissolved_values": _claims(entity, "P576"),
                           "source_batch": "|".join(sorted(path.stem for path in store.directory.glob("wdqs-batch-*.json")))})
    domain_owner: dict[str, str] = {}
    for candidate in candidates:
        candidate["duplicate_domain_of_qid"] = domain_owner.get(candidate["normalized_domain"], "")
        domain_owner.setdefault(candidate["normalized_domain"], candidate["qid"])

    verification_path = ROOT / "data/phase4c1_website_verification.csv"
    settings = WebsiteSettings(user_agent=UA, request_timeout_seconds=10, delay_between_requests_seconds=0.5,
                               max_pages_per_company=1, max_companies_per_run=target, retry_count=0,
                               max_response_bytes=1_000_000)
    verification_by_qid = {}
    if verification_path.exists():
        with verification_path.open(encoding="utf-8", newline="") as handle:
            verification_by_qid = {row["qid"]: row for row in csv.DictReader(handle)}
    terminal_statuses = {"VERIFIED", "AMBIGUOUS", "REJECTED_THIRD_PARTY", "REJECTED_PARKED", "FETCH_FAILED", "ROBOTS_DENIED", "BLOCKED"}
    missing_verifications = [row for row in candidates if row["qid"] not in verification_by_qid or verification_by_qid[row["qid"]].get("status") not in terminal_statuses]
    if missing_verifications and not offline:
        collector = WebsiteCollector(settings)
        try:
            for index, candidate in enumerate(missing_verifications, 1):
                verification_by_qid[candidate["qid"]] = _verify_site(collector, candidate["qid"], candidate["company_name"], candidate["website_url"], index)
                write_csv_atomic(verification_path, ["qid", "company_name", "website_url", "status", "http_status", "final_url", "page_title", "website_signals", "evidence"], list(verification_by_qid.values()))
        finally:
            collector.close()
    if offline:
        for candidate in missing_verifications:
            verification_by_qid.setdefault(candidate["qid"], {"qid": candidate["qid"], "company_name": candidate["company_name"], "website_url": candidate["website_url"], "status": "NOT_CHECKED_OFFLINE", "http_status": "", "final_url": "", "page_title": "", "website_signals": "{}", "evidence": "no saved verification result; offline replay made no request"})
    verification = [verification_by_qid[row["qid"]] for row in candidates]

    verify_by_qid = {row["qid"]: row for row in verification}
    rules = load_yaml(ROOT / "config/qualification.yaml")
    for candidate in candidates:
        verification_row = verify_by_qid.get(candidate["qid"], {})
        candidate["website_status"] = verification_row.get("status", "NOT_CHECKED")
        numbers = _employee_numbers(candidate["employee_values"])
        candidate["employee_size_evidence"] = candidate["employee_values"] or "MISSING"
        candidate["clearly_large_enterprise"] = str(any(value >= 1000 for value in numbers)).lower()
        candidate["potential_smb_candidate"] = str(candidate["website_status"] == "VERIFIED" and not any(value >= 1000 for value in numbers)).lower()
        candidate["human_review_required"] = str(candidate["website_status"] != "VERIFIED" or not numbers or any(value >= 1000 for value in numbers)).lower()
        try:
            website_evidence = json.loads(verification_row.get("website_signals") or "{}")
        except json.JSONDecodeError:
            website_evidence = {}
        qualification = evaluate({"discovery_source": True, "active": True, "website_evidence": website_evidence,
                                  "fetch_status": "SUCCESS" if website_evidence else "FETCH_FAILED"}, rules)
        candidate["qualification_eligibility"] = qualification.eligibility
        candidate["geo_opportunity"] = qualification.geo_opportunity
        candidate["priority_tier"] = qualification.priority_tier

    pilot_fields = ["qid", "company_name", "website_url", "normalized_domain", "entity_url", "industry_qids", "country_qids", "admin_qids", "headquarters_qids", "employee_values", "employee_size_evidence", "clearly_large_enterprise", "potential_smb_candidate", "human_review_required", "duplicate_domain_of_qid", "website_status", "qualification_eligibility", "geo_opportunity", "priority_tier", "source_batch"]
    write_csv_atomic(ROOT / "data/phase4c1_wikidata_pilot.csv", pilot_fields, candidates)
    write_csv_atomic(verification_path, ["qid", "company_name", "website_url", "status", "http_status", "final_url", "page_title", "website_signals", "evidence"], verification)
    count_domains = len({row["normalized_domain"] for row in candidates})
    return {"successful_query_batches": batch_count, "failed_queries": failures, "retry_count": retries,
            "raw_binding_rows": raw_binding_rows, "distinct_qids_retrieved": len(records),
            "records_retrieved": len(candidates), "unique_qids": len({row["qid"] for row in candidates}),
            "unique_domains": count_domains, "verified_websites": sum(row["status"] == "VERIFIED" for row in verification),
            "ambiguous_websites": sum(row["status"] == "AMBIGUOUS" for row in verification),
            "large_enterprises": sum(row["clearly_large_enterprise"] == "true" for row in candidates),
            "potential_smb_candidates_for_human_size_review": sum(row["potential_smb_candidate"] == "true" for row in candidates),
            "human_review": sum(row["human_review_required"] == "true" for row in candidates),
            "duplicate_domains": len(candidates) - count_domains}


def run(args: argparse.Namespace) -> None:
    store = SnapshotStore(args.snapshot_dir)
    start = time.monotonic()
    retries = failures = 0
    if not args.offline:
        with httpx.Client(headers={"User-Agent": UA, "Accept": "application/sparql-results+json"}, follow_redirects=True) as client:
            retries, failures = _fetch_batches(client, store, args.target, args.batch_size, offline=False)
    result = export_pilot(store=store, target=args.target, retries=retries, failures=failures, offline=args.offline)
    result["runtime_seconds"] = round(time.monotonic() - start, 2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--offline", action="store_true", help="Replay saved successful snapshots only")
    parser.add_argument("--snapshot-dir", type=Path, default=SNAPSHOTS)
    run(parser.parse_args())
