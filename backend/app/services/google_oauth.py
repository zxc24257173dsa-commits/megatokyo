"""Google sign-in (OpenID Connect authorization-code flow).

Security measures:
  * state  - random value bound to a short-lived signed cookie (stops login CSRF)
  * nonce  - echoed inside the signed ID token (stops token replay)
  * PKCE   - code_verifier / S256 challenge (stops stolen authorization codes being used)
  * ID token signature checked against Google's published keys; issuer, audience, expiry,
    nonce, email_verified (and optional Workspace domain) are all verified
  * The client secret never leaves the server.
"""
import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import jwt

from app.core import security
from app.core.config import get_settings
from app.core.errors import AppError

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
STATE_COOKIE = "erp_google_oauth"
STATE_COOKIE_PATH = "/api/v1/auth/google"
STATE_TTL = timedelta(minutes=10)

_jwks_client: jwt.PyJWKClient | None = None


def redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/api/v1/auth/google/callback"


def require_enabled() -> None:
    if not get_settings().google_enabled:
        raise AppError("google_not_configured", "Google sign-in is not configured", 404)


def safe_next(path: str | None) -> str:
    """Only same-site relative paths (no open redirect to another site)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def begin(next_path: str | None) -> tuple[str, str]:
    """Returns (Google authorize URL, signed state cookie value)."""
    require_enabled()
    s = get_settings()
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": s.google_client_id, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    }
    if s.google_hosted_domain:
        params["hd"] = s.google_hosted_domain
    cookie = security._encode(  # signed + expiring, same key as our JWTs
        {"sub": "google-oauth", "typ": "oauth_state", "tv": 0, "state": state, "nonce": nonce, "verifier": verifier,
         "next": safe_next(next_path)},
        STATE_TTL,
    )
    return f"{AUTHORIZE_URL}?{urlencode(params)}", cookie


def read_state_cookie(cookie: str | None, state: str | None) -> dict:
    claims = security.decode_token(cookie or "", "oauth_state")
    if claims is None or not state or not secrets.compare_digest(claims["state"], state):
        raise AppError("google_state_invalid", "Sign-in session expired; please try again", 400)
    return claims


def exchange_code(code: str, verifier: str) -> str:
    """Trade the authorization code for tokens; returns the raw ID token."""
    s = get_settings()
    try:
        r = httpx.post(TOKEN_URL, timeout=10, data={
            "code": code, "client_id": s.google_client_id,
            "client_secret": s.google_client_secret.get_secret_value(),
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier,
        })
    except httpx.HTTPError as exc:
        raise AppError("google_unreachable", "Could not reach Google; please try again", 502) from exc
    if r.status_code != 200 or "id_token" not in r.json():
        raise AppError("google_exchange_failed", "Google rejected the sign-in; please try again", 400)
    return r.json()["id_token"]


def _signing_key(id_token: str):
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwks_client.get_signing_key_from_jwt(id_token).key


def verify_id_token(id_token: str, nonce: str) -> dict:
    s = get_settings()
    try:
        claims = jwt.decode(
            id_token, _signing_key(id_token), algorithms=["RS256"], audience=s.google_client_id,
            options={"require": ["iss", "aud", "exp", "iat", "sub", "email"]}, leeway=60,
        )
    except jwt.PyJWTError as exc:
        raise AppError("google_token_invalid", "Google sign-in could not be verified", 400) from exc
    if claims.get("iss") not in ISSUERS:
        raise AppError("google_token_invalid", "Google sign-in could not be verified", 400)
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise AppError("google_token_invalid", "Google sign-in could not be verified", 400)
    if claims.get("email_verified") is not True:
        raise AppError("google_email_unverified", "This Google account's email is not verified", 403)
    if s.google_hosted_domain and claims.get("hd") != s.google_hosted_domain:
        raise AppError("google_domain_not_allowed", "This Google account is not in the allowed domain", 403)
    return claims
