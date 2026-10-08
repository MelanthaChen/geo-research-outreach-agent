from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class WebsiteSettings(BaseModel):
    user_agent: str = "GEOResearchOutreachAgent/0.2 (academic research; no outreach)"
    request_timeout_seconds: float = 15
    delay_between_requests_seconds: float = 1.0
    max_pages_per_company: int = 4
    max_companies_per_run: int = 25
    retry_count: int = 1
    cache_ttl_hours: int = 168
    max_response_bytes: int = 2_000_000


class Settings(BaseModel):
    database_url: str = "sqlite:///data/outreach.db"
    log_level: str = "INFO"
    send_mode: str = "dry_run"
    website: WebsiteSettings = WebsiteSettings()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_settings(path: Path = Path("config/settings.yaml")) -> Settings:
    values = load_yaml(path) if path.exists() else {}
    overrides = {
        "database_url": os.getenv("OUTREACH_DATABASE_URL"),
        "log_level": os.getenv("OUTREACH_LOG_LEVEL"),
        "send_mode": os.getenv("SEND_MODE"),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    settings = Settings.model_validate(values)
    if settings.send_mode not in {"dry_run", "test", "production"}:
        raise ValueError("SEND_MODE must be dry_run, test, or production")
    return settings
