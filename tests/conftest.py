import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from outreach_agent.models import Base


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session

