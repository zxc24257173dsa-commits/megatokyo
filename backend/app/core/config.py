"""Application settings, loaded from environment variables (or a local .env file).

Secrets (DB password, token-encryption key, Whatnot credentials) are never
hard-coded: in production they are injected from GCP Secret Manager as env vars.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="ERP_", extra="ignore")

    env: Literal["dev", "test", "staging", "production"] = "dev"
    app_name: str = "Whatnot Global ERP"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = Field(
        default="postgresql+psycopg://erp:erp_dev_pw@localhost:5432/erp_dev",
        description="SQLAlchemy URL (psycopg 3 driver).",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 30_000

    # Business-wide constants confirmed by the owner (2026-11-09).
    base_currency: str = "USD"
    business_timezone: str = "America/New_York"
    week_start: Literal["monday"] = "monday"

    cors_origins: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()
