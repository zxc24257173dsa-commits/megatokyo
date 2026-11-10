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

    # ---- Google sign-in ----
    # Create an OAuth client (type "Web application") in Google Cloud Console and set
    # ERP_GOOGLE_CLIENT_ID / ERP_GOOGLE_CLIENT_SECRET. Redirect URI to register:
    #   {public_base_url}/api/v1/auth/google/callback
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_hosted_domain: str | None = None  # optional: only accounts of this Google Workspace domain
    public_base_url: str = "http://localhost:8000"
    password_login_enabled: bool = True  # backup login; Google-only users have no password anyway

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    # ---- Whatnot Seller API (Phase 5) ----
    # Token: a personal access token (wn_pat_tk_...) saved encrypted from the admin screen, or
    # ERP_WHATNOT_API_TOKEN (Secret Manager in production). Webhook secret: wn_wh_...
    whatnot_api_base: str = "https://api.whatnot.com/seller-api/rest/v2"
    whatnot_api_token: SecretStr | None = None
    whatnot_webhook_secret: SecretStr | None = None
    whatnot_requests_per_second: float = 8.0  # Whatnot allows 10/s per seller per app
    whatnot_sync_days: int = 3  # how far back each order/shipment sync looks
    whatnot_financial_days: int = 14  # financial report window per sync (fees post when an order ships)

    # ---- Business flow ----
    # Owner (2026-11-09): goods are shipped from Japan straight to the US; no Japan warehouse.
    # When false the UI hides the Japan warehouse and JP_MAIN stock operations are refused.
    japan_warehouse_enabled: bool = False

    # ---- File storage (Phase 3) ----
    # "local" stores under storage_dir (dev / single server); "gcs" = private Cloud Storage bucket (production).
    storage_backend: Literal["local", "gcs"] = "local"
    storage_bucket: str | None = None
    # Shared secret Cloud Scheduler sends to /api/v1/internal/cron/* (Phase 12). Unset = those endpoints are off.
    cron_secret: SecretStr | None = None
    # Cloud Run (request-based billing) pauses work done after a response: run the "sync now" button inside the
    # request, and leave webhook events to the every-minute scheduler instead of a background task.
    inline_jobs: bool = False
    storage_dir: str = "var/storage"
    max_image_bytes: int = 5 * 1024 * 1024
    file_url_ttl_seconds: int = 3600

    # Built SPA (frontend/dist). Served by the API in production so both share one origin.
    frontend_dist: str = "../frontend/dist"

    @model_validator(mode="after")
    def _no_dev_secrets_outside_dev(self):
        if self.env in ("staging", "production"):
            if self.jwt_secret.get_secret_value() == _DEV_JWT_SECRET:
                raise ValueError("ERP_JWT_SECRET must be set in staging/production")
            if self.data_encryption_key.get_secret_value() == _DEV_DATA_KEY:
                raise ValueError("ERP_DATA_ENCRYPTION_KEY must be set in staging/production")
            if not self.cookie_secure:
                raise ValueError("ERP_COOKIE_SECURE must be true in staging/production")
            if self.storage_backend == "local":
                raise ValueError("ERP_STORAGE_BACKEND must be gcs in staging/production (Cloud Run disks are wiped)")
            if self.storage_backend == "gcs" and not self.storage_bucket:
                raise ValueError("ERP_STORAGE_BUCKET must be set when ERP_STORAGE_BACKEND=gcs")
            if self.public_base_url.startswith("http://"):
                raise ValueError("ERP_PUBLIC_BASE_URL must be https in staging/production")
            if self.cron_secret is not None and len(self.cron_secret.get_secret_value()) < 32:
                raise ValueError("ERP_CRON_SECRET must be at least 32 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
