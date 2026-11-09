"""External platform integration (Whatnot Seller API): credentials, webhook inbox,
sync runs, and raw payload archive (every number can be traced to its source payload)."""
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin
from app.models._util import check_in


class IntegrationAccount(IdMixin, TimestampMixin, Base):
    """Tokens are stored encrypted (AES-GCM, key from Secret Manager); never returned to clients."""

    __tablename__ = "integration_accounts"

    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    seller_id: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(120))
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    webhook_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="inactive")

    __table_args__ = (
        UniqueConstraint("platform", "environment", "seller_id", postgresql_nulls_not_distinct=True),
        check_in("environment", ("staging", "production"), "env_valid"),
        check_in("status", ("inactive", "active", "error", "revoked"), "status_valid"),
    )


WEBHOOK_STATUSES = ("pending", "processing", "processed", "failed", "ignored")


class WebhookEvent(IdMixin, Base):
    __tablename__ = "webhook_events"

    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    seller_id: Mapped[str | None] = mapped_column(String(128))
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("platform", "dedupe_key"),
        check_in("status", WEBHOOK_STATUSES, "status_valid"),
        Index("ix_webhook_events_status_received", "status", "received_at"),
    )


class SyncRun(IdMixin, Base):
    __tablename__ = "sync_runs"

    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    cursor: Mapped[str | None] = mapped_column(Text)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        check_in("status", ("running", "succeeded", "failed"), "status_valid"),
        Index("ix_sync_runs_kind_started", "platform", "kind", "started_at"),
    )


class ExternalRawRecord(IdMixin, Base):
    """Every distinct version of an external record we received (API, webhook or CSV row)."""

    __tablename__ = "external_raw_records"

    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("platform", "entity_type", "external_id", "payload_hash"),
        check_in("source", ("api", "webhook", "csv"), "source_valid"),
    )
