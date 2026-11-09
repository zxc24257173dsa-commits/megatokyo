"""User administration (users.manage). Every change is audited with before/after values.
Safety rules: you cannot deactivate yourself, and the last active admin can never be removed."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import security
from app.core.errors import AppError
from app.models.auth import Role, User, user_roles
from app.services import audit
from app.services.audit import RequestContext
from app.services.auth import normalize_email, revoke_all_sessions, role_codes, validate_new_password

SUPPORTED_LOCALES = ("zh-TW", "ja", "en")
_ADMIN_GUARD_LOCK = 7_420_001  # pg advisory lock id serialising admin-membership changes


def _snapshot(db: Session, user: User) -> dict:
    return {
        "email": user.email, "display_name": user.display_name, "locale": user.locale, "timezone": user.timezone,
        "is_active": user.is_active, "mfa_enabled": user.mfa_enabled, "roles": role_codes(db, user.id),
        "has_password": user.password_hash is not None, "google_linked": user.google_sub is not None,
    }


def _validate_profile(locale: str | None, timezone: str | None) -> None:
    if locale is not None and locale not in SUPPORTED_LOCALES:
        raise AppError("invalid_locale", f"locale must be one of {SUPPORTED_LOCALES}", 422)
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise AppError("invalid_timezone", "Unknown IANA timezone", 422) from None


def _role_ids(db: Session, codes: list[str]) -> list[int]:
    if not codes:
        raise AppError("roles_required", "A user needs at least one role", 422)
    rows = dict(db.execute(select(Role.code, Role.id).where(Role.code.in_(codes))).all())
    unknown = sorted(set(codes) - set(rows))
    if unknown:
        raise AppError("unknown_role", "Unknown role(s)", 422, {"roles": unknown})
    return list(rows.values())


def _ensure_another_active_admin(db: Session, user_id: int) -> None:
    db.execute(select(func.pg_advisory_xact_lock(_ADMIN_GUARD_LOCK)))
    others = db.scalar(
        select(func.count()).select_from(User)
        .join(user_roles, user_roles.c.user_id == User.id)
        .join(Role, Role.id == user_roles.c.role_id)
        .where(Role.code == "admin", User.is_active, User.id != user_id)
    )
    if not others:
        raise AppError("last_admin", "This is the last active administrator; add another admin first", 409)


def create_user(
    db: Session, *, actor_id: int | None, email: str, display_name: str, password: str | None, roles: list[str],
    locale: str = "zh-TW", timezone: str = "America/New_York", must_change_password: bool = True,
    ctx: RequestContext | None = None,
) -> User:
    email = normalize_email(email)
    _validate_profile(locale, timezone)
    if password is not None:
        validate_new_password(password, email)
    role_ids = _role_ids(db, roles)
    if db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
        raise AppError("email_taken", "A user with this email already exists", 409)
    # password=None -> Google sign-in only.
    user = User(email=email, display_name=display_name.strip(),
                password_hash=security.hash_password(password) if password is not None else None,
                locale=locale, timezone=timezone, must_change_password=must_change_password and password is not None)
    db.add(user)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:  # concurrent creation with the same email
        db.expunge(user)
        raise AppError("email_taken", "A user with this email already exists", 409) from None
    db.execute(insert(user_roles), [{"user_id": user.id, "role_id": rid} for rid in role_ids])
    audit.record(db, action="user.created", entity_type="user", entity_id=user.id, actor_id=actor_id,
                 after=_snapshot(db, user), ctx=ctx)
    return user


def get_user(db: Session, user_id: int, *, for_update: bool = False) -> User:
    user = db.get(User, user_id, with_for_update=for_update, populate_existing=for_update)
    if user is None:
        raise AppError("not_found", "User not found", 404)
    return user


def update_user(
    db: Session, *, actor_id: int, user_id: int, display_name: str | None = None, locale: str | None = None,
    timezone: str | None = None, is_active: bool | None = None, roles: list[str] | None = None,
    ctx: RequestContext | None = None,
) -> User:
    user = get_user(db, user_id, for_update=True)
    before = _snapshot(db, user)
    _validate_profile(locale, timezone)

    losing_admin = "admin" in before["roles"] and (
        (is_active is False) or (roles is not None and "admin" not in roles)
    )
    if is_active is False and user_id == actor_id:
        raise AppError("cannot_deactivate_self", "You cannot deactivate your own account", 409)
    if losing_admin:
        _ensure_another_active_admin(db, user_id)

    if display_name is not None:
        user.display_name = display_name.strip()
    if locale is not None:
        user.locale = locale
    if timezone is not None:
        user.timezone = timezone
    if roles is not None:
        role_ids = _role_ids(db, roles)
        db.execute(delete(user_roles).where(user_roles.c.user_id == user.id))
        db.execute(insert(user_roles), [{"user_id": user.id, "role_id": rid} for rid in role_ids])
        if set(roles) != set(before["roles"]):
            revoke_all_sessions(db, user, "roles_changed")  # new permissions apply on next login
    if is_active is not None and is_active != user.is_active:
        user.is_active = is_active
        if not is_active:
            revoke_all_sessions(db, user, "deactivated")
    db.flush()
    after = _snapshot(db, user)
    if after != before:
        audit.record(db, action="user.updated", entity_type="user", entity_id=user.id, actor_id=actor_id,
                     before=before, after=after, ctx=ctx)
    return user


def admin_reset_password(db: Session, *, actor_id: int, user_id: int, temporary_password: str,
                         ctx: RequestContext | None = None) -> None:
    user = get_user(db, user_id, for_update=True)
    validate_new_password(temporary_password, user.email)
    user.password_hash = security.hash_password(temporary_password)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None
    revoke_all_sessions(db, user, "password_reset")
    audit.record(db, action="user.password_reset", entity_type="user", entity_id=user.id, actor_id=actor_id, ctx=ctx)


def admin_reset_mfa(db: Session, *, actor_id: int, user_id: int, ctx: RequestContext | None = None) -> None:
    """For a lost phone. The user must enrol again at next login (if their role requires MFA)."""
    user = get_user(db, user_id, for_update=True)
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    user.mfa_pending_secret_enc = None
    user.mfa_last_used_step = None
    revoke_all_sessions(db, user, "mfa_reset")
    audit.record(db, action="user.mfa_reset", entity_type="user", entity_id=user.id, actor_id=actor_id, ctx=ctx)


def admin_unlock(db: Session, *, actor_id: int, user_id: int, ctx: RequestContext | None = None) -> None:
    user = get_user(db, user_id, for_update=True)
    user.failed_login_count = 0
    user.locked_until = None
    audit.record(db, action="user.unlocked", entity_type="user", entity_id=user.id, actor_id=actor_id, ctx=ctx)
