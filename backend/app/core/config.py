"""Application settings, loaded from environment variables (or a local .env file).

Secrets (DB password, token-encryption key, Whatnot credentials) are never
hard-coded: in production they are injected from GCP Secret Manager as env vars.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-0123456789"
_DEV_DATA_KEY = "ZGV2LW9ubHktaW5zZWN1cmUta2V5LTMyLWJ5dGVzISE="  # base64("dev-only-insecure-key-32-bytes!!")


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

    # ---- Authentication (Phase 2) ----
    # Dev-only defaults; startup refuses to run in staging/production with these values.
    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    # base64 of 32 random bytes; encrypts MFA secrets now and Whatnot API tokens in Phase 5.
    data_encryption_key: SecretStr = SecretStr(_DEV_DATA_KEY)
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14
    mfa_challenge_ttl_minutes: int = 5
    refresh_cookie_name: str = "erp_refresh"
    cookie_secure: bool = True
    login_max_failures: int = 5
    lockout_minutes: int = 15
    password_min_length: int = 12
    mfa_required_roles: list[str] = ["admin"]
    mfa_issuer: str = "Megatokyo ERP"

    @model_validator(mode="after")
    def _no_dev_secrets_outside_dev(self):
        if self.env in ("staging", "production"):
            if self.jwt_secret.get_secret_value() == _DEV_JWT_SECRET:
                raise ValueError("ERP_JWT_SECRET must be set in staging/production")
            if self.data_encryption_key.get_secret_value() == _DEV_DATA_KEY:
                raise ValueError("ERP_DATA_ENCRYPTION_KEY must be set in staging/production")
            if not self.cookie_secure:
                raise ValueError("ERP_COOKIE_SECURE must be true in staging/production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
