import time
from datetime import UTC, datetime, timedelta

import pyotp
from sqlalchemy import select, text

from app.models.auth import AuditLog
from tests.conftest import STRONG_PW, auth_header, enroll_mfa, login, make_user


def actions(db, user_id):
    return [a for a in db.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == str(user_id)).order_by(AuditLog.id))]


def test_login_success_returns_token_and_httponly_refresh_cookie(api, db):
    u = make_user(db)
    r = login(api, u.email.upper())  # email is case-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is False and body["token"]["token_type"] == "bearer"
    assert "refresh_token" not in str(body)
    cookie = r.headers["set-cookie"]
    assert "erp_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/api/v1/auth" in cookie
    me = api.get("/api/v1/auth/me", headers=auth_header(body["token"]["access_token"])).json()
    assert me["email"] == u.email and me["roles"] == ["read_only"] and "catalog.view" in me["permissions"]
    assert "auth.login" in actions(db, u.id)


def test_wrong_password_and_unknown_email_look_identical(api, db):
    u = make_user(db)
    a = login(api, u.email, "Wrong-password-123")
    b = login(api, "nobody@example.com", "Wrong-password-123")
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["message"] == b.json()["error"]["message"]
    assert a.json()["error"]["code"] == "invalid_credentials"


def test_lockout_after_repeated_failures(api, db):
    u = make_user(db)
    for _ in range(5):
        assert login(api, u.email, "Wrong-password-123").status_code == 401
    assert login(api, u.email).status_code == 401  # correct password, but locked
    db.execute(text("UPDATE users SET locked_until = now() - interval '1 second' WHERE id = :i"), {"i": u.id})
    assert login(api, u.email).status_code == 200  # lock expired


def test_no_auth_header_or_bad_token_is_401(api):
    assert api.get("/api/v1/auth/me").status_code == 401
    assert api.get("/api/v1/auth/me", headers=auth_header("garbage")).status_code == 401


def test_refresh_rotation_and_reuse_detection(api, db):
    u = make_user(db)
    login(api, u.email)
    first_cookie = api.cookies.get("erp_refresh")
    r = api.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    second_cookie = api.cookies.get("erp_refresh")
    assert second_cookie != first_cookie

    # Replaying the old token long after rotation = theft -> whole session family revoked.
    db.execute(text("UPDATE refresh_tokens SET revoked_at = now() - interval '5 minutes' "
                    "WHERE revoked_reason = 'rotated' AND user_id = :u"), {"u": u.id})
    api.cookies.clear()
    api.cookies.set("erp_refresh", first_cookie, path="/api/v1/auth")
    assert api.post("/api/v1/auth/refresh").status_code == 401
    api.cookies.clear()
    api.cookies.set("erp_refresh", second_cookie, path="/api/v1/auth")
    assert api.post("/api/v1/auth/refresh").status_code == 401  # legit token also dead now
    assert "auth.refresh_reuse_detected" in actions(db, u.id)


def test_logout_revokes_refresh(api, db):
    u = make_user(db)
    login(api, u.email)
    cookie = api.cookies.get("erp_refresh")
    assert api.post("/api/v1/auth/logout").status_code == 204
    api.cookies.set("erp_refresh", cookie, path="/api/v1/auth")
    assert api.post("/api/v1/auth/refresh").status_code == 401


def test_must_change_password_restricts_until_changed(api, db):
    admin = make_user(db, roles=["tw_manager"], must_change=True)
    token = login(api, admin.email).json()["token"]
    assert token["restrictions"] == ["password_change_required"]
    r = api.get("/api/v1/audit-logs", headers=auth_header(token["access_token"]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "password_change_required"

    weak = api.post("/api/v1/auth/password", headers=auth_header(token["access_token"]),
                    json={"current_password": STRONG_PW, "new_password": "short"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "weak_password"
    r = api.post("/api/v1/auth/password", headers=auth_header(token["access_token"]),
                 json={"current_password": STRONG_PW, "new_password": "Another-Strong-77#"})
    assert r.status_code == 200 and r.json()["restrictions"] == []
    # The old access token was invalidated by the password change; the new one works.
    assert api.get("/api/v1/auth/me", headers=auth_header(token["access_token"])).status_code == 401
    assert api.get("/api/v1/audit-logs", headers=auth_header(r.json()["access_token"])).status_code == 200


def test_admin_must_enroll_mfa_then_login_requires_code(api, db):
    admin = make_user(db, roles=["admin"])
    token = login(api, admin.email).json()["token"]
    assert token["restrictions"] == ["mfa_setup_required"]
    r = api.get("/api/v1/users", headers=auth_header(token["access_token"]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "mfa_setup_required"

    secret = enroll_mfa(api, token["access_token"])
    row = db.execute(text("SELECT mfa_secret_enc FROM users WHERE id = :i"), {"i": admin.id}).scalar_one()
    assert secret.encode() not in bytes(row)  # stored encrypted

    first = login(api, admin.email).json()
    assert first["mfa_required"] is True and first["token"] is None
    bad = api.post("/api/v1/auth/mfa/verify", json={"mfa_token": first["mfa_token"], "code": "000000"})
    assert bad.status_code == 401

    next_code = pyotp.TOTP(secret).at(time.time() + 30)  # the enable step was already used
    ok = api.post("/api/v1/auth/mfa/verify", json={"mfa_token": first["mfa_token"], "code": next_code})
    assert ok.status_code == 200 and ok.json()["restrictions"] == []
    assert api.get("/api/v1/users", headers=auth_header(ok.json()["access_token"])).status_code == 200

    again = login(api, admin.email).json()
    replay = api.post("/api/v1/auth/mfa/verify", json={"mfa_token": again["mfa_token"], "code": next_code})
    assert replay.status_code == 401  # same code cannot be used twice


def test_mfa_guesses_count_toward_lockout(api, db):
    admin = make_user(db, roles=["admin"])
    enroll_mfa(api, login(api, admin.email).json()["token"]["access_token"])
    for _ in range(5):
        challenge = login(api, admin.email).json()["mfa_token"]
        api.post("/api/v1/auth/mfa/verify", json={"mfa_token": challenge, "code": "000000"})
    assert login(api, admin.email).status_code == 401  # locked


def test_deactivation_takes_effect_immediately(api, db):
    from app.services import users
    actor = make_user(db, roles=["tw_manager"])
    victim = make_user(db)
    access = login(api, victim.email).json()["token"]["access_token"]
    assert api.get("/api/v1/auth/me", headers=auth_header(access)).status_code == 200
    users.update_user(db, actor_id=actor.id, user_id=victim.id, is_active=False)
    db.commit()
    assert api.get("/api/v1/auth/me", headers=auth_header(access)).status_code == 401
    assert login(api, victim.email).status_code == 401


def test_expired_access_token_rejected(api, db, monkeypatch):
    import jwt as pyjwt

    from app.core.config import get_settings
    u = make_user(db)
    expired = pyjwt.encode(
        {"sub": str(u.id), "typ": "access", "tv": 1, "iat": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
         "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())},
        get_settings().jwt_secret.get_secret_value(), algorithm="HS256")
    assert api.get("/api/v1/auth/me", headers=auth_header(expired)).status_code == 401
