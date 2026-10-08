import json
import csv
from pathlib import Path

from outreach_agent.models import Company, DiscoveryRecord, ReviewStatus, WebsiteSnapshot
from outreach_agent.qualification import qualify_companies
from outreach_agent.cli import app
from outreach_agent.database import init_database
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner


def test_requalification_preserves_human_review(session):
    company = Company(
        company_name="Reviewed", normalized_name="reviewed", website="https://reviewed.example",
        normalized_domain="reviewed.example", company_size_category="SMALL",
        review_status=ReviewStatus.REJECTED, review_notes="Professor excluded this company",
    )
    session.add(company)
    session.flush()
    session.add(DiscoveryRecord(company_id=company.id, source_type="test", source_name="Test", source_identifier="1", raw_data="{}"))
    session.add(WebsiteSnapshot(
        company_id=company.id, requested_url=company.website, final_url=company.website, http_status=200,
        fetch_status="SUCCESS", visible_text_length=2000,
        extracted_signals=json.dumps({
            "reachable": True, "meaningful_text": True, "substantial_public_information": True,
            "online_discovery_relevance": True, "pages_inspected": 2,
        }),
    ))
    session.commit()
    qualify_companies(session, Path("config/qualification.yaml"), force=True)
    session.refresh(company)
    assert company.review_status == ReviewStatus.REJECTED
    assert company.review_notes == "Professor excluded this company"
    assert company.priority_score is not None


def test_review_csv_import_only_changes_allowlisted_fields(tmp_path):
    db_path = tmp_path / "review.db"
    engine = create_engine(f"sqlite:///{db_path}")
    init_database(engine)
    with Session(engine) as session:
        company = Company(company_name="Canonical Name", normalized_name="canonical name", website="https://canonical.example", normalized_domain="canonical.example")
        session.add(company)
        session.commit()
        company_id = company.id
    csv_path = tmp_path / "review.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["company_id", "company_name", "website", "review_status", "review_notes", "reviewed_by"])
        writer.writeheader()
        writer.writerow({"company_id": company_id, "company_name": "Tampered", "website": "https://tampered.example", "review_status": "APPROVED", "review_notes": "Good fit", "reviewed_by": "professor"})
    result = CliRunner().invoke(app, ["import-review", "--source", str(csv_path)], env={"OUTREACH_DATABASE_URL": f"sqlite:///{db_path}"})
    assert result.exit_code == 0, result.output
    with Session(engine) as session:
        company = session.get(Company, company_id)
        assert company.company_name == "Canonical Name"
        assert company.website == "https://canonical.example"
        assert company.review_status == ReviewStatus.APPROVED
        assert company.review_notes == "Good fit"
