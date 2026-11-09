"""Authentication: login (+ lockout), MFA (TOTP), refresh-token rotation, password change,
and the user's effective roles / permissions / restrictions."""
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import crypto, security
from app.core.config import get_settings
from app.core.errors import AppError
from app.models.auth import Permission, RefreshToken, Role, User, role_permissions, user_roles
from app.services import audit
from app.services.audit import RequestContext

def _invalid_login() -> AppError:
    # One message for unknown email, wrong password, inactive and locked: no account enumeration.
    return AppError("invalid_credentials", "Email or password is incorrect, or the account is temporarily locked", 401)

# Restrictions: a user with any of these can only use self-service auth endpoints until resolved.
PASSWORD_CHANGE_REQUIRED = "password_change_required"
MFA_SETUP_REQUIRED = "mfa_setup_required"


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def mfa_context(user_id: int) -> str:
    return f"user:{user_id}:mfa"


# ---------- roles / permissions ----------

def role_codes(db: Session, user_id: int) -> list[str]:
    q = select(Role.code).join(user_roles, user_roles.c.role_id == Role.id).where(user_roles.c.user_id == user_id)
    return sorted(db.scalars(q))


def permission_codes(db: Session, user_id: int) -> set[str]:
    q = (
        select(Permission.code)
        .join(role_permissions, role_permissions.c.permission_id == Permission.id)
        .join(user_roles, user_roles.c.role_id == role_permissions.c.role_id)
        .where(user_roles.c.user_id == user_id)
    )
    return set(db.scalars(q))


def restrictions(user: User, roles: list[str], auth_method: str = "password") -> list[str]:
    """Google sessions are already protected by Google (use 2-Step Verification on the Google
    account), so they skip our TOTP and temporary-password rules."""
    out = []
    if auth_method == "google":
        return out
    if user.must_change_password:
        out.append(PASSWORD_CHANGE_REQUIRED)
    if not user.mfa_enabled and set(roles) & set(get_settings().mfa_required_roles):
        out.append(MFA_SETUP_REQUIRED)
    return out


# ---------- sessions ----------

@dataclass
class IssuedSession:
    access_token: str
    expires_in: int
    refresh_token: str
    restrictions: list[str] = field(default_factory=list)


def _issue_session(db: Session, user: User, ctx: RequestContext, family_id: str | None = None,
                   auth_method: str = "password") -> tuple[IssuedSession, RefreshToken]:
    access, ttl = security.create_access_token(user.id, user.token_version, auth_method)
    raw, hashed = security.new_refresh_token()
    rt = RefreshToken(
        user_id=user.id, token_hash=hashed, family_id=family_id or uuid.uuid4().hex, auth_method=auth_method,
        expires_at=_now() + timedelta(days=get_settings().refresh_token_ttl_days),
        ip=ctx.ip, user_agent=(ctx.user_agent or "")[:500] or None,
    )
    db.add(rt)
    db.flush()
    return IssuedSession(access, ttl, raw, restrictions(user, role_codes(db, user.id), auth_method)), rt


def revoke_all_sessions(db: Session, user: User, reason: str) -> None:
    """Invalidate every refresh token and (via token_version) every access token of the user."""
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason=reason)
    )
    user.token_version += 1


def _register_failure(db: Session, user: User, ctx: RequestContext, reason: str) -> None:
    s = get_settings()
    user.failed_login_count += 1
    locked = False
    if user.failed_login_count >= s.login_max_failures:
        user.locked_until = _now() + timedelta(minutes=s.lockout_minutes)
        user.failed_login_count = 0
        locked = True
    audit.record(db, action="auth.login_failed", entity_type="user", entity_id=user.id, actor_id=user.id,
                 after={"reason": reason, "locked": locked}, ctx=ctx)
    db.commit()  # persist the failure even though the request fails


def _is_locked(user: User) -> bool:
    return user.locked_until is not None and user.locked_until > _now()


def _complete_login(db: Session, user: User, ctx: RequestContext, method: str) -> IssuedSession:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _now()
    session, _ = _issue_session(db, user, ctx, auth_method=method)
    audit.record(db, action="auth.login", entity_type="user", entity_id=user.id, actor_id=user.id,
                 after={"method": method, "restrictions": session.restrictions}, ctx=ctx)
    return session


@dataclass
class LoginResult:
    session: IssuedSession | None = None
    mfa_token: str | None = None


def login(db: Session, email: str, password: str, ctx: RequestContext) -> LoginResult:
    if not get_settings().password_login_enabled:
        raise AppError("password_login_disabled", "Password sign-in is disabled; use Google sign-in", 403)
    email = normalize_email(email)
    user = db.scalar(select(User).where(func.lower(User.email) == email).with_for_update().execution_options(populate_existing=True))
    if user is None:
        security.verify_password(password, None)  # constant-ish time
        audit.record(db, action="auth.login_failed", entity_type="user", after={"email": email, "reason": "unknown_user"},
                     ctx=ctx)
        db.commit()
        raise _invalid_login()
    if not user.is_active or _is_locked(user):
        security.verify_password(password, None)
        audit.record(db, action="auth.login_failed", entity_type="user", entity_id=user.id, actor_id=user.id,
                     after={"reason": "inactive" if not user.is_active else "locked"}, ctx=ctx)
        db.commit()
        raise _invalid_login()
    if not security.verify_password(password, user.password_hash):
        _register_failure(db, user, ctx, "bad_password")
        raise _invalid_login()

    if security.password_needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)

    if user.mfa_enabled:
        # Failure counter is NOT reset here: MFA guesses keep counting toward lockout.
        db.commit()
        return LoginResult(mfa_token=security.create_mfa_challenge_token(user.id, user.token_version))
    return LoginResult(session=_complete_login(db, user, ctx, "password"))



def verify_mfa_login(db: Session, mfa_token: str, code: str, ctx: RequestContext) -> IssuedSession:
    claims = security.decode_token(mfa_token, "mfa")
    if claims is None:
        raise AppError("mfa_challenge_invalid", "MFA challenge expired or invalid; please log in again", 401)
    user = db.get(User, int(claims["sub"]), with_for_update=True, populate_existing=True)
    if user is None or not user.is_active or user.token_version != claims["tv"] or not user.mfa_enabled:
        raise AppError("mfa_challenge_invalid", "MFA challenge expired or invalid; please log in again", 401)
    if _is_locked(user):
        raise _invalid_login()
    secret = crypto.decrypt(user.mfa_secret_enc, context=mfa_context(user.id))
    step = security.verify_totp(secret, code, user.mfa_last_used_step)
    if step is None:
        _register_failure(db, user, ctx, "bad_mfa_code")
        raise AppError("mfa_code_invalid", "Authentication code is incorrect", 401)
    user.mfa_last_used_step = step
    return _complete_login(db, user, ctx, "password+totp")


def refresh(db: Session, raw_refresh: str | None, ctx: RequestContext) -> IssuedSession:
    invalid = AppError("refresh_invalid", "Session expired; please log in again", 401)
    if not raw_refresh:
        raise invalid
    rt = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == security.hash_refresh_token(raw_refresh)).with_for_update()
    )
    if rt is None:
        raise invalid
    user = db.get(User, rt.user_id)
    if rt.revoked_at is not None:
        # A rotated token presented again within a few seconds is usually two browser tabs
        # refreshing at once; anything else is treated as theft and the whole session family dies.
        benign = rt.revoked_reason == "rotated" and rt.revoked_at > _now() - timedelta(seconds=30)
        if not benign:
            db.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=_now(), revoked_reason="reuse_detected")
            )
            audit.record(db, action="auth.refresh_reuse_detected", entity_type="user", entity_id=rt.user_id,
                         actor_id=rt.user_id, after={"family_id": rt.family_id}, ctx=ctx)
            db.commit()
        raise invalid
    if rt.expires_at <= _now() or user is None or not user.is_active:
        raise invalid
    session, new_rt = _issue_session(db, user, ctx, family_id=rt.family_id, auth_method=rt.auth_method)
    rt.revoked_at = _now()
    rt.revoked_reason = "rotated"
    rt.replaced_by_id = new_rt.id
    return session


def logout(db: Session, raw_refresh: str | None, ctx: RequestContext) -> None:
    if not raw_refresh:
        return
    rt = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == security.hash_refresh_token(raw_refresh)))
    if rt is None:
        return
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == rt.family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now(), revoked_reason="logout")
    )
    audit.record(db, action="auth.logout", entity_type="user", entity_id=rt.user_id, actor_id=rt.user_id, ctx=ctx)


# ---------- self-service ----------

def validate_new_password(password: str, email: str) -> None:
    errors = security.password_policy_errors(password, email=email)
    if errors:
        raise AppError("weak_password", "Password does not meet the policy", 422, {"rules": errors})


def change_password(db: Session, user: User, current: str, new: str, ctx: RequestContext,
                    auth_method: str = "password") -> IssuedSession:
    if user.password_hash is None:
        raise AppError("no_password", "This account signs in with Google and has no password", 409)
    if not security.verify_password(current, user.password_hash):
        _register_failure(db, user, ctx, "bad_password_on_change")
        raise AppError("invalid_credentials", "Current password is incorrect", 401)
    if current == new:
        raise AppError("weak_password", "New password must differ from the current one", 422)
    validate_new_password(new, user.email)
    user.password_hash = security.hash_password(new)
    user.must_change_password = False
    user.password_changed_at = _now()
    revoke_all_sessions(db, user, "password_changed")
    audit.record(db, action="auth.password_changed", entity_type="user", entity_id=user.id, actor_id=user.id, ctx=ctx)
    session, _ = _issue_session(db, user, ctx, auth_method=auth_method)  # keep this device signed in
    return session


def mfa_begin_setup(db: Session, user: User, ctx: RequestContext) -> tuple[str, str]:
    if user.mfa_enabled:
        raise AppError("mfa_already_enabled", "Two-factor authentication is already enabled", 409)
    secret = security.new_totp_secret()
    user.mfa_pending_secret_enc = crypto.encrypt(secret, context=mfa_context(user.id))
    audit.record(db, action="auth.mfa_setup_started", entity_type="user", entity_id=user.id, actor_id=user.id, ctx=ctx)
    return secret, security.totp_provisioning_uri(secret, user.email)


def mfa_enable(db: Session, user: User, code: str, ctx: RequestContext) -> None:
    if user.mfa_enabled:
        raise AppError("mfa_already_enabled", "Two-factor authentication is already enabled", 409)
    if user.mfa_pending_secret_enc is None:
        raise AppError("mfa_setup_not_started", "Start MFA setup first", 409)
    secret = crypto.decrypt(user.mfa_pending_secret_enc, context=mfa_context(user.id))
    step = security.verify_totp(secret, code, None)
    if step is None:
        raise AppError("mfa_code_invalid", "Authentication code is incorrect", 422)
    user.mfa_secret_enc = user.mfa_pending_secret_enc
    user.mfa_pending_secret_enc = None
    user.mfa_enabled = True
    user.mfa_last_used_step = step
    audit.record(db, action="auth.mfa_enabled", entity_type="user", entity_id=user.id, actor_id=user.id, ctx=ctx)


def login_with_google(db: Session, claims: dict, ctx: RequestContext) -> IssuedSession:
    """Only users an admin has already added (by email) can sign in; the Google account id
    is bound on first use so a later owner of the same address cannot take over."""
    email = normalize_email(claims["email"])
    user = db.scalar(select(User).where(func.lower(User.email) == email).with_for_update()
                     .execution_options(populate_existing=True))
    if user is None or not user.is_active:
        audit.record(db, action="auth.login_failed", entity_type="user", entity_id=user.id if user else None,
                     actor_id=user.id if user else None,
                     after={"email": email, "method": "google",
                            "reason": "inactive" if user else "not_registered"}, ctx=ctx)
        db.commit()
        raise AppError("account_not_registered", "This Google account has not been added to the ERP", 403)
    if user.google_sub is None:
        taken = db.scalar(select(User.id).where(User.google_sub == claims["sub"]))
        if taken is not None:
            raise AppError("google_account_in_use", "This Google account is linked to another user", 409)
        user.google_sub = claims["sub"]
        audit.record(db, action="auth.google_linked", entity_type="user", entity_id=user.id, actor_id=user.id,
                     after={"email": email}, ctx=ctx)
    elif user.google_sub != claims["sub"]:
        audit.record(db, action="auth.login_failed", entity_type="user", entity_id=user.id, actor_id=user.id,
                     after={"method": "google", "reason": "google_account_mismatch"}, ctx=ctx)
        db.commit()
        raise AppError("google_account_mismatch", "This email is linked to a different Google account", 403)
    return _complete_login(db, user, ctx, "google")


def mfa_disable(db: Session, user: User, password: str, code: str, ctx: RequestContext) -> None:
    if not user.mfa_enabled:
        raise AppError("mfa_not_enabled", "Two-factor authentication is not enabled", 409)
    secret = crypto.decrypt(user.mfa_secret_enc, context=mfa_context(user.id))
    step = security.verify_totp(secret, code, user.mfa_last_used_step)
    if not security.verify_password(password, user.password_hash) or step is None:
        _register_failure(db, user, ctx, "bad_mfa_disable")
        raise AppError("invalid_credentials", "Password or authentication code is incorrect", 401)
    user.mfa_enabled = False
    user.mfa_secret_enc = None
    user.mfa_last_used_step = None
    revoke_all_sessions(db, user, "mfa_disabled")
    audit.record(db, action="auth.mfa_disabled", entity_type="user", entity_id=user.id, actor_id=user.id, ctx=ctx)
