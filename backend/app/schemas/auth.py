from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Email = Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    """Refresh token is NOT in the body: it is set as an httpOnly cookie."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    restrictions: list[str] = []


class LoginResponse(BaseModel):
    mfa_required: bool = False
    mfa_token: str | None = None
    token: TokenResponse | None = None


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=8)


class MfaCode(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=8)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    locale: str
    timezone: str
    mfa_enabled: bool
    last_login_at: datetime | None
    roles: list[str]
    permissions: list[str]
    restrictions: list[str]
