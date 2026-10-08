import csv
from pathlib import Path

from sqlalchemy import func, select

from outreach_agent.discovery import CSVDiscoverySource, JSONDiscoverySource
from outreach_agent.models import Company, DiscoveryRecord
from outreach_agent.services import ingest
from outreach_agent.qualification import qualify_companies


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_deduplication_idempotency_and_provenance(session, tmp_path):
    path = tmp_path / "companies.csv"
    write_csv(path, [
        {"company_name": "Acme", "website": "https://www.example.com/a", "source_url": "list:a"},
        {"company_name": "Acme Inc", "website": "http://example.com/", "source_url": "list:b"},
    ])
    first = ingest(session, CSVDiscoverySource(path))
    second = ingest(session, CSVDiscoverySource(path))

    assert first.created == 1
    assert first.duplicates == 1
    assert second.created == 0
    assert second.duplicates == 2
    assert session.scalar(select(func.count(Company.id))) == 1
    assert session.scalar(select(func.count(DiscoveryRecord.id))) == 2
    assert second.provenance_existing == 2


def test_json_discovery(session, tmp_path):
    path = tmp_path / "companies.json"
    path.write_text('[{"company_name":"JSON Co","website":"json.example"}]', encoding="utf-8")
    summary = ingest(session, JSONDiscoverySource(path))
    assert summary.created == 1
    assert session.scalar(select(Company)).normalized_domain == "json.example"


def test_qualification_is_resumable(session, tmp_path):
    source_path = tmp_path / "companies.json"
    source_path.write_text('[{"company_name":"Ready Co","website":"ready.example"}]', encoding="utf-8")
    ingest(session, JSONDiscoverySource(source_path))
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "thresholds: {qualified: 55, needs_review: 30}\n"
        "weights: {has_website: 60, customer_facing: 0, search_dependent: 0, content_rich: 0, smb_or_startup: 0, active: 0, geo_opportunity: 0}\n"
        "penalties: {dead_website: -60, unsuitable: -50, large_enterprise: -25}\n",
        encoding="utf-8",
    )
    first = qualify_companies(session, rules_path)
    second = qualify_companies(session, rules_path)
    assert first["processed"] == 1
    assert second["processed"] == 0
