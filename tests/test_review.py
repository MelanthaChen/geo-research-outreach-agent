import json
import csv
from pathlib import Path

from outreach_agent.models import Company, DiscoveryRecord, ReviewStatus, WebsiteSnapshot
from outreach_agent.models import BusinessChannelType, Contact, ContactType, ContactabilityStatus
from outreach_agent.qualification import qualify_companies
from outreach_agent.selection import export_company_selection
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


def test_repeated_qualification_is_idempotent_and_preserves_channel_override(session):
    company = Company(company_name="Repeat", normalized_name="repeat", website="https://repeat.example",
                      normalized_domain="repeat.example", review_status=ReviewStatus.MAYBE,
                      review_notes="Research activity", channel_override_contact_id=987,
                      channel_override_reason="Documented reviewer selection")
    session.add(company)
    session.flush()
    session.add(DiscoveryRecord(company_id=company.id, source_type="test", source_name="Test",
                                source_identifier="repeat-1", raw_data="{}"))
    session.commit()
    qualify_companies(session, Path("config/qualification.yaml"), force=True)
    first = (company.eligibility, company.geo_opportunity, company.priority_score, company.priority_tier)
    qualify_companies(session, Path("config/qualification.yaml"), force=True)
    second = (company.eligibility, company.geo_opportunity, company.priority_score, company.priority_tier)
    assert first == second
    assert company.review_status == ReviewStatus.MAYBE
    assert company.review_notes == "Research activity"
    assert company.channel_override_contact_id == 987
    assert company.channel_override_reason == "Documented reviewer selection"


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


def test_selection_export_is_stable_and_keeps_unknowns_blank(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'selection.db'}")
    init_database(engine)
    output_a, summary_a = tmp_path / "a.csv", tmp_path / "summary-a.csv"
    output_b, summary_b = tmp_path / "b.csv", tmp_path / "summary-b.csv"
    with Session(engine) as session:
        company = Company(company_name="Unknowns", normalized_name="unknowns", website="https://unknowns.example",
                          normalized_domain="unknowns.example", review_status=ReviewStatus.MAYBE,
                          review_notes="Need source confirmation")
        session.add(company)
        session.flush()
        session.add(DiscoveryRecord(company_id=company.id, source_type="test", source_name="Test source",
                                    source_identifier="stable-1", raw_data="{}"))
        session.commit()
        export_company_selection(session, Path("config/qualification.yaml"), output_a, summary_a)
        export_company_selection(session, Path("config/qualification.yaml"), output_b, summary_b)
    assert output_a.read_bytes() == output_b.read_bytes()
    assert summary_a.read_bytes() == summary_b.read_bytes()
    with output_a.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["company_id"] == "1"
    assert row["company_size"] == ""
    assert row["geography"] == ""
    assert row["industry"] == ""
    assert row["eligibility"] == "NEEDS_REVIEW"
    assert row["company_review_status"] == "MAYBE"
    assert row["reviewer_notes"] == "Need source confirmation"


def test_company_and_channel_reviews_are_distinct_on_override(tmp_path):
    db_path = tmp_path / "override.db"
    engine = create_engine(f"sqlite:///{db_path}")
    init_database(engine)
    with Session(engine) as session:
        company = Company(company_name="Override", normalized_name="override", website="https://override.example",
                          normalized_domain="override.example", review_status=ReviewStatus.REJECTED,
                          review_notes="Do not select for study")
        session.add(company)
        session.flush()
        contact = Contact(company_id=company.id, email="business@override.example",
                          contact_type=ContactType.GENERIC_BUSINESS_CONTACT,
                          channel_type=BusinessChannelType.GENERAL_BUSINESS_EMAIL,
                          source_url="https://override.example/contact", review_status=ReviewStatus.PENDING)
        session.add(contact)
        session.commit()
        company_id, contact_id = company.id, contact.id
    result = CliRunner().invoke(app, ["review", "override-channel", str(company_id), str(contact_id),
                                      "--reason", "Official contact page names this as the business inbox"],
                                env={"OUTREACH_DATABASE_URL": f"sqlite:///{db_path}"})
    assert result.exit_code == 0, result.output
    with Session(engine) as session:
        company = session.get(Company, company_id)
        contact = session.get(Contact, contact_id)
        assert company.review_status == ReviewStatus.REJECTED
        assert company.review_notes == "Do not select for study"
        assert company.primary_channel_id is None
        assert company.channel_override_contact_id == contact_id
        assert company.channel_override_reason.startswith("Official contact page")
        assert contact.review_status == ReviewStatus.MAYBE


def test_selection_export_uses_override_but_preserves_machine_primary(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'override-export.db'}")
    init_database(engine)
    with Session(engine) as session:
        company = Company(company_name="Company", normalized_name="company", website="https://company.example",
                          normalized_domain="company.example")
        session.add(company)
        session.flush()
        machine = Contact(company_id=company.id, email="info@company.example", channel_type=BusinessChannelType.GENERAL_BUSINESS_EMAIL,
                          contact_type=ContactType.GENERIC_BUSINESS_CONTACT, review_status=ReviewStatus.PENDING)
        override = Contact(company_id=company.id, channel_url="https://forms.example/form", channel_type=BusinessChannelType.CONTACT_FORM,
                           contact_type=ContactType.GENERIC_BUSINESS_CONTACT, review_status=ReviewStatus.MAYBE)
        session.add_all([machine, override])
        session.flush()
        company.primary_channel_id = machine.id
        company.channel_override_contact_id = override.id
        company.channel_override_reason = "Official form better fits the research request"
        session.commit()
        out = tmp_path / "selection.csv"
        export_company_selection(session, Path("config/qualification.yaml"), out)
        with out.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        assert row["primary_channel"] == "https://forms.example/form"
        assert row["channel_review_status"] == "MAYBE"
        assert company.primary_channel_id == machine.id


def test_unified_csv_import_updates_company_and_channel_decisions_only(tmp_path):
    db_path = tmp_path / "roundtrip.db"
    engine = create_engine(f"sqlite:///{db_path}")
    init_database(engine)
    with Session(engine) as session:
        company = Company(company_name="Canonical", normalized_name="canonical", website="https://canonical.example",
                          normalized_domain="canonical.example", review_notes="Old company note")
        session.add(company)
        session.flush()
        channel = Contact(company_id=company.id, email="team@canonical.example",
                          contact_type=ContactType.GENERIC_BUSINESS_CONTACT,
                          channel_type=BusinessChannelType.GENERAL_BUSINESS_EMAIL,
                          review_status=ReviewStatus.PENDING)
        session.add(channel)
        session.flush()
        company.primary_channel_id = channel.id
        session.commit()
        company_id, channel_id = company.id, channel.id
    csv_path = tmp_path / "unified.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "company_id", "company_name", "website", "company_review_status", "reviewer_notes",
            "reviewed_by", "channel_review_status",
        ])
        writer.writeheader()
        writer.writerow({"company_id": company_id, "company_name": "Tampered", "website": "https://other.example",
                         "company_review_status": "APPROVED", "reviewer_notes": "Ready for study review",
                         "reviewed_by": "reviewer", "channel_review_status": "MAYBE"})
    result = CliRunner().invoke(app, ["import-review", "--source", str(csv_path)],
                                env={"OUTREACH_DATABASE_URL": f"sqlite:///{db_path}"})
    assert result.exit_code == 0, result.output
    with Session(engine) as session:
        company = session.get(Company, company_id)
        channel = session.get(Contact, channel_id)
        assert company.company_name == "Canonical"
        assert company.website == "https://canonical.example"
        assert company.review_status == ReviewStatus.APPROVED
        assert company.review_notes == "Ready for study review"
        assert channel.review_status == ReviewStatus.MAYBE
