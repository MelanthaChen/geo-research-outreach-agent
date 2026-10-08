from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from outreach_agent.models import Base


def create_db_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url)


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    # Lightweight Phase 1 -> Phase 2 migration. A formal migration tool can follow
    # once schema changes become frequent.
    if "discovery_records" in inspect(engine).get_table_names():
        columns = {column["name"] for column in inspect(engine).get_columns("discovery_records")}
        with engine.begin() as connection:
            if "source_name" not in columns:
                connection.execute(text("ALTER TABLE discovery_records ADD COLUMN source_name VARCHAR(255)"))
            if "source_url" not in columns:
                connection.execute(text("ALTER TABLE discovery_records ADD COLUMN source_url VARCHAR(2048)"))


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
