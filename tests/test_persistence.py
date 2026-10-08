from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from outreach_agent.models import Base, Company, CompanyStatus


def test_database_persistence(tmp_path):
    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Company(company_name="Persisted", normalized_name="persisted", website="https://persisted.example", normalized_domain="persisted.example"))
        session.commit()
    with Session(engine) as session:
        company = session.scalar(select(Company))
        assert company.company_name == "Persisted"
        assert company.pipeline_status == CompanyStatus.DISCOVERED

