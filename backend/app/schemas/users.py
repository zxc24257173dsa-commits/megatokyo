from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.auth import Email


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    locale: str
    timezone: str
    is_active: bool
    mfa_enabled: bool
    must_change_password: bool
    locked: bool
    has_password: bool
    google_linked: bool
    last_login_at: datetime | None
    created_at: datetime
    roles: list[str]


class UserCreate(BaseModel):
    email: Email
    display_name: str = Field(min_length=1, max_length=120)
    # Leave empty for Google-only accounts.
    temporary_password: str | None = Field(default=None, min_length=1, max_length=256)
    roles: list[str] = Field(min_length=1)
    locale: str = "zh-TW"
    timezone: str = "America/New_York"


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    locale: str | None = None
    timezone: str | None = None
    is_active: bool | None = None
    roles: list[str] | None = Field(default=None, min_length=1)


class PasswordReset(BaseModel):
    temporary_password: str = Field(min_length=1, max_length=256)


class RoleOut(BaseModel):
    code: str
    name: str
    description: str | None
    permissions: list[str]


class AuditLogOut(BaseModel):
    id: int
    created_at: datetime
    user_id: int | None
    action: str
    entity_type: str
    entity_id: str | None
    before: dict | None
    after: dict | None
    ip: str | None
    request_id: str | None


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    next_before_id: int | None
