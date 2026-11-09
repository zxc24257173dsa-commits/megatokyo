"""Google sign-in tests. Google itself is replaced by a local test signer (an RSA key generated
here); only the two network calls (token exchange, Google's public keys) are swapped out -
all verification logic under test is the production code."""
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sqlalchemy import select, text

from app.core.config import get_settings
from app.models.auth import AuditLog
from app.services import google_oauth
from tests.conftest import STRONG_PW, auth_header, login, make_user

CLIENT_ID = "test-client.apps.googleusercontent.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def google(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(s, "google_client_secret", SecretStr("test-secret"))
    monkeypatch.setattr(s, "google_hosted_domain", None)
    monkeypatch.setattr(google_oauth, "_signing_key", lambda token: _KEY.public_key())
    idp = {"claims": {}, "verifiers": []}

    def fake_exchange(code, verifier):
        idp["verifiers"].append(verifier)
        return idp["token"]

    monkeypatch.setattr(google_oauth, "exchange_code", fake_exchange)
    return idp


def id_token(nonce, *, email, sub="google-sub-1", **over):
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": sub, "email": email,
              "email_verified": True, "nonce": nonce, "iat": now, "exp": now + 3600, **over}
    return jwt.encode(claims, _KEY, algorithm="RS256")


def google_login(api, idp, *, email, next_path=None, state_override=None, nonce_override=None, **claims):
    r = api.get("/api/v1/auth/google/start", params={"next": next_path} if next_path else None, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    idp["challenge"] = q["code_challenge"][0]
    idp["token"] = id_token(nonce_override or q["nonce"][0], email=email, **claims)
    cb = api.get("/api/v1/auth/google/callback", params={"code": "auth-code", "state": state_override or q["state"][0]},
                 follow_redirects=False)
    assert cb.status_code == 302
    return cb.headers["location"], q


def test_config_endpoint(api, google):
    assert api.get("/api/v1/auth/config").json() == {"google_enabled": True, "password_enabled": True}


def test_start_redirects_to_google_with_state_nonce_and_pkce(api, google):
    r = api.get("/api/v1/auth/google/start", follow_redirects=False)
    url = urlparse(r.headers["location"])
    q = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert q["client_id"] == [CLIENT_ID] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://localhost:8000/api/v1/auth/google/callback"]
    assert q["state"][0] and q["nonce"][0]
    cookie = r.headers["set-cookie"]
    assert "erp_google_oauth=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_admin_signs_in_with_google_without_codes(api, db, google):
    admin = make_user(db, roles=["admin"])  # no MFA enrolled
    location, q = google_login(api, google, email=admin.email.upper(), next_path="/stock/counts")
    assert location == "/stock/counts"
    # PKCE: the verifier sent to Google matches the challenge in the authorize URL.
    v = google["verifiers"][-1]
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"][0]

    tok = api.post("/api/v1/auth/refresh").json()
    assert tok["restrictions"] == []  # Google session: no TOTP enrolment demanded
    me = api.get("/api/v1/auth/me", headers=auth_header(tok["access_token"])).json()
    assert me["auth_method"] == "google" and me["restrictions"] == []
    assert api.get("/api/v1/users", headers=auth_header(tok["access_token"])).status_code == 200
    # session method survives refresh rotation
    tok2 = api.post("/api/v1/auth/refresh").json()
    assert api.get("/api/v1/auth/me", headers=auth_header(tok2["access_token"])).json()["auth_method"] == "google"
    actions = list(db.scalars(select(AuditLog.action).where(AuditLog.entity_id == str(admin.id))))
    assert "auth.google_linked" in actions and "auth.login" in actions


def test_unregistered_google_account_is_rejected(api, db, google):
    location, _ = google_login(api, google, email="stranger@gmail.com")
    assert location == "/?login_error=account_not_registered"
    assert api.post("/api/v1/auth/refresh").status_code == 401


def test_deactivated_user_is_rejected(api, db, google):
    from app.services import users
    actor = make_user(db, roles=["tw_manager"])
    u = make_user(db)
    users.update_user(db, actor_id=actor.id, user_id=u.id, is_active=False)
    db.commit()
    location, _ = google_login(api, google, email=u.email)
    assert location == "/?login_error=account_not_registered"


@pytest.mark.parametrize("kwargs,code", [
    ({"state_override": "forged-state"}, "google_state_invalid"),
    ({"nonce_override": "replayed-nonce"}, "google_token_invalid"),
    ({"email_verified": False}, "google_email_unverified"),
    ({"aud": "someone-elses-client"}, "google_token_invalid"),
    ({"iss": "https://evil.example.com"}, "google_token_invalid"),
    ({"exp": int(time.time()) - 3600}, "google_token_invalid"),
])
def test_invalid_sign_ins_are_rejected(api, db, google, kwargs, code):
    u = make_user(db)
    location, _ = google_login(api, google, email=u.email, **kwargs)
    assert location == f"/?login_error={code}"
    assert api.post("/api/v1/auth/refresh").status_code == 401


def test_google_account_is_bound_on_first_use(api, db, google):
    u = make_user(db)
    assert google_login(api, google, email=u.email, sub="sub-A")[0] == "/"
    api.cookies.clear()
    location, _ = google_login(api, google, email=u.email, sub="sub-B")
    assert location == "/?login_error=google_account_mismatch"


def test_hosted_domain_restriction(api, db, google, monkeypatch):
    monkeypatch.setattr(get_settings(), "google_hosted_domain", "megatokyo.jp")
    u = make_user(db)
    assert google_login(api, google, email=u.email)[0] == "/?login_error=google_domain_not_allowed"
    api.cookies.clear()
    assert google_login(api, google, email=u.email, hd="megatokyo.jp")[0] == "/"


def test_no_open_redirect(api, db, google):
    u = make_user(db)
    for evil in ("//evil.com/x", "https://evil.com", "/\\evil.com"):
        api.cookies.clear()
        assert google_login(api, google, email=u.email, next_path=evil)[0] == "/"


def test_google_only_user_has_no_password_login(api, db, google):
    mgr = make_user(db, roles=["admin"])
    google_login(api, google, email=mgr.email, sub="admin-sub")
    admin_tok = api.post("/api/v1/auth/refresh").json()["access_token"]
    r = api.post("/api/v1/users", headers=auth_header(admin_tok), json={
        "email": "osaka@example.com", "display_name": "大阪", "roles": ["jp_inventory"]})
    assert r.status_code == 201 and r.json()["has_password"] is False and r.json()["must_change_password"] is False
    assert login(api, "osaka@example.com", "anything-at-all-123").status_code == 401

    api.cookies.clear()
    google_login(api, google, email="osaka@example.com", sub="osaka-sub")
    tok = api.post("/api/v1/auth/refresh").json()["access_token"]
    r = api.post("/api/v1/auth/password", headers=auth_header(tok),
                 json={"current_password": "x", "new_password": "Brand-New-Pass-2026"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_password"


def test_password_login_can_be_disabled(api, db, monkeypatch):
    u = make_user(db)
    monkeypatch.setattr(get_settings(), "password_login_enabled", False)
    r = login(api, u.email, STRONG_PW)
    assert r.status_code == 403 and r.json()["error"]["code"] == "password_login_disabled"


def test_google_not_configured(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "google_client_id", None)
    assert api.get("/api/v1/auth/config").json()["google_enabled"] is False
    assert api.get("/api/v1/auth/google/start", follow_redirects=False).status_code == 404


def test_password_sessions_still_require_admin_mfa(api, db):
    admin = make_user(db, roles=["admin"])
    tok = login(api, admin.email).json()["token"]
    assert tok["restrictions"] == ["mfa_setup_required"]
    assert db.execute(text("SELECT auth_method FROM refresh_tokens WHERE user_id = :u"), {"u": admin.id}).scalar_one() == "password"
