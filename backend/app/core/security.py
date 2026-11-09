"""Password hashing (Argon2id), JWT access / MFA-challenge tokens, opaque refresh tokens, TOTP."""
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.config import get_settings

_ph = PasswordHasher()  # Argon2id with library defaults (OWASP-compliant)
# Used to spend the same time when the user does not exist (prevents account enumeration by timing).
_DUMMY_HASH = _ph.hash("dummy-password-for-timing")

ALGORITHM = "HS256"
TOTP_INTERVAL = 30


# ---------- passwords ----------

def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def password_policy_errors(password: str, *, email: str | None = None) -> list[str]:
    s = get_settings()
    errors = []
    if len(password) < s.password_min_length:
        errors.append(f"at least {s.password_min_length} characters")
    if len(password) > 128:
        errors.append("at most 128 characters")
    if password.strip() != password:
        errors.append("no leading or trailing spaces")
    local = email.split("@")[0].lower() if email else ""
    if len(local) >= 3 and local in password.lower():
        errors.append("must not contain the email name")
    if len(set(password)) < 5:
        errors.append("too few distinct characters")
    return errors


# ---------- JWT ----------

def _encode(claims: dict, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {**claims, "iat": int(now.timestamp()), "exp": int((now + ttl).timestamp()), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, get_settings().jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def create_access_token(user_id: int, token_version: int, auth_method: str = "password") -> tuple[str, int]:
    """`amr` records how this session signed in (password / password+totp / google)."""
    ttl = timedelta(minutes=get_settings().access_token_ttl_minutes)
    claims = {"sub": str(user_id), "typ": "access", "tv": token_version, "amr": auth_method}
    return _encode(claims, ttl), int(ttl.total_seconds())


def create_mfa_challenge_token(user_id: int, token_version: int) -> str:
    ttl = timedelta(minutes=get_settings().mfa_challenge_ttl_minutes)
    return _encode({"sub": str(user_id), "typ": "mfa", "tv": token_version}, ttl)


def decode_token(token: str, expected_type: str) -> dict | None:
    try:
        claims = jwt.decode(
            token, get_settings().jwt_secret.get_secret_value(), algorithms=[ALGORITHM],
            options={"require": ["sub", "exp", "iat", "typ", "tv"]},
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != expected_type:
        return None
    return claims


# ---------- refresh tokens (opaque, stored hashed) ----------

def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------- TOTP ----------

def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=get_settings().mfa_issuer)


def verify_totp(secret: str, code: str, last_used_step: int | None, now: float | None = None) -> int | None:
    """Accepts the current, previous or next 30-second step (clock drift). Returns the matched
    step, or None. A step that is <= last_used_step is rejected (no code replay)."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // TOTP_INTERVAL)
    for step in (current - 1, current, current + 1):
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(totp.at(step * TOTP_INTERVAL), code):
            return step
    return None
