"""Users, roles, permissions, refresh tokens, audit log."""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Table,
    Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # NULL = Google sign-in only (no password login possible).
    password_hash: Mapped[str | None] = mapped_column(Text)
    # Google account "sub" (stable id), bound at first Google login so a recycled email can't take over.
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False, server_default="zh-TW")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="America/New_York")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    mfa_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_pending_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # during enrolment
    mfa_last_used_step: Mapped[int | None] = mapped_column(BigInteger)  # TOTP replay protection
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Lockout after repeated failures (password or MFA).
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Admin-set passwords must be changed at first login.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Bumped on password change / deactivation / forced logout: invalidates all issued access tokens.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        Index("uq_users_email_lower", func.lower(email), unique=True),
        CheckConstraint("failed_login_count >= 0", name="failed_nonneg"),
        CheckConstraint("NOT mfa_enabled OR mfa_secret_enc IS NOT NULL", name="mfa_has_secret"),
    )


class Role(IdMixin, CreatedMixin, Base):
    __tablename__ = "roles"

    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class Permission(IdMixin, CreatedMixin, Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", BigInteger, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)


class RefreshToken(IdMixin, CreatedMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    family_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # one per login session
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False, server_default="password")
    revoked_reason: Mapped[str | None] = mapped_column(String(30))
    replaced_by_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_tokens.id"))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("auth_method IN ('password', 'password+totp', 'google')", name="auth_method_valid"),
    )


class AuditLog(IdMixin, CreatedMixin, Base):
    """Append-only (enforced by trigger)."""

    __tablename__ = "audit_logs"

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(80))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_user_id", "user_id"),
    )
