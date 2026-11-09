"""Authentication / authorisation dependencies.

The user, roles and permissions are re-read from the database on every request, so
deactivation, role changes and password changes take effect immediately everywhere
(Japan, US, Taiwan) - nothing is cached in the browser token except the user id and
token_version.
"""
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core import security
from app.core.errors import AppError
from app.core.logging import request_id_var
from app.db.session import get_db
from app.models.auth import User
from app.services import auth as auth_service
from app.services.audit import RequestContext


def get_request_context(request: Request) -> RequestContext:
    return RequestContext(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=request_id_var.get(),
    )


@dataclass
class CurrentUser:
    user: User
    roles: list[str]
    permissions: set[str]
    restrictions: list[str]

    @property
    def id(self) -> int:
        return self.user.id


def _unauthorized() -> AppError:
    return AppError("unauthorized", "Authentication required", 401)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    """Any authenticated user, including ones with pending restrictions
    (used only by self-service auth endpoints)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    claims = security.decode_token(token, "access")
    if claims is None:
        raise _unauthorized()
    user = db.get(User, int(claims["sub"]))
    if user is None or not user.is_active or user.token_version != claims["tv"]:
        raise _unauthorized()
    roles = auth_service.role_codes(db, user.id)
    return CurrentUser(user, roles, auth_service.permission_codes(db, user.id), auth_service.restrictions(user, roles))


def get_active_user(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Authenticated user with no pending restriction (password change / MFA enrolment)."""
    if current.restrictions:
        first = current.restrictions[0]
        msg = {
            auth_service.PASSWORD_CHANGE_REQUIRED: "You must change your password before continuing",
            auth_service.MFA_SETUP_REQUIRED: "Your role requires two-factor authentication; set it up first",
        }[first]
        raise AppError(first, msg, 403)
    return current


def require_permissions(*codes: str):
    """Dependency factory: all listed permissions are required."""

    def _dep(current: CurrentUser = Depends(get_active_user)) -> CurrentUser:
        missing = [c for c in codes if c not in current.permissions]
        if missing:
            raise AppError("forbidden", "You do not have permission for this action", 403, {"missing": missing})
        return current

    return _dep
