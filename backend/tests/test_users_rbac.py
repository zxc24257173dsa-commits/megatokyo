import time

import pyotp
from sqlalchemy import select

from app.models.auth import AuditLog
from tests.conftest import STRONG_PW, auth_header, enroll_mfa, login, make_user


def admin_token(api, db):
    """A fully set-up admin (MFA enrolled) and their access token."""
    admin = make_user(db, roles=["admin"])
    secret = enroll_mfa(api, login(api, admin.email).json()["token"]["access_token"])
    challenge = login(api, admin.email).json()["mfa_token"]
    r = api.post("/api/v1/auth/mfa/verify",
                 json={"mfa_token": challenge, "code": pyotp.TOTP(secret).at(time.time() + 30)})
    return admin, r.json()["access_token"]


def token_for(api, db, role):
    u = make_user(db, roles=[role])
    return u, login(api, u.email).json()["token"]["access_token"]


def test_permission_matrix_on_endpoints(api, db):
    expectations = {
        # role: (can manage users, can view audit)
        "jp_inventory": (False, False),
        "us_live_ops": (False, False),
        "tw_manager": (False, True),
        "read_only": (False, False),
    }
    for role, (users_ok, audit_ok) in expectations.items():
        _, tok = token_for(api, db, role)
        assert (api.get("/api/v1/users", headers=auth_header(tok)).status_code == 200) is users_ok, role
        assert (api.get("/api/v1/audit-logs", headers=auth_header(tok)).status_code == 200) is audit_ok, role
    _, tok = admin_token(api, db)
    assert api.get("/api/v1/users", headers=auth_header(tok)).status_code == 200
    assert api.get("/api/v1/audit-logs", headers=auth_header(tok)).status_code == 200


def test_forbidden_response_lists_missing_permission(api, db):
    _, tok = token_for(api, db, "read_only")
    r = api.post("/api/v1/users", headers=auth_header(tok), json={})
    assert r.status_code == 403
    assert r.json()["error"]["details"]["missing"] == ["users.manage"]


def test_admin_creates_user_who_must_change_password(api, db):
    _, tok = admin_token(api, db)
    r = api.post("/api/v1/users", headers=auth_header(tok), json={
        "email": "Tokyo.Staff@Example.com", "display_name": "東京倉庫", "temporary_password": "Temp-Password-2026",
        "roles": ["jp_inventory"], "locale": "ja", "timezone": "Asia/Tokyo"})
    assert r.status_code == 201, r.text
    u = r.json()
    assert u["email"] == "tokyo.staff@example.com" and u["must_change_password"] and u["roles"] == ["jp_inventory"]
    dup = api.post("/api/v1/users", headers=auth_header(tok), json={
        "email": "tokyo.staff@example.com", "display_name": "x", "temporary_password": "Temp-Password-2026",
        "roles": ["read_only"]})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"
    staff_login = login(api, "tokyo.staff@example.com", "Temp-Password-2026").json()
    assert staff_login["token"]["restrictions"] == ["password_change_required"]


def test_create_user_validation(api, db):
    _, tok = admin_token(api, db)
    base = {"email": "a@example.com", "display_name": "A", "temporary_password": "Temp-Password-2026",
            "roles": ["read_only"]}
    for patch, code in [
        ({"roles": ["superuser"]}, "unknown_role"),
        ({"temporary_password": "weak"}, "weak_password"),
        ({"timezone": "Mars/Base"}, "invalid_timezone"),
        ({"locale": "fr"}, "invalid_locale"),
    ]:
        r = api.post("/api/v1/users", headers=auth_header(tok), json={**base, **patch})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, (patch, r.text)


def test_role_change_is_audited_with_before_and_after(api, db):
    admin, tok = admin_token(api, db)
    u = make_user(db, roles=["read_only"])
    r = api.patch(f"/api/v1/users/{u.id}", headers=auth_header(tok), json={"roles": ["us_live_ops"]})
    assert r.status_code == 200 and r.json()["roles"] == ["us_live_ops"]
    entry = db.scalars(select(AuditLog).where(AuditLog.action == "user.updated", AuditLog.entity_id == str(u.id))).one()
    assert entry.user_id == admin.id
    assert entry.before["roles"] == ["read_only"] and entry.after["roles"] == ["us_live_ops"]
    assert "password_hash" not in entry.after


def test_role_change_ends_existing_sessions(api, db):
    _, tok = admin_token(api, db)
    u, utok = token_for(api, db, "read_only")
    api.patch(f"/api/v1/users/{u.id}", headers=auth_header(tok), json={"roles": ["tw_manager"]})
    assert api.get("/api/v1/auth/me", headers=auth_header(utok)).status_code == 401


def test_last_admin_cannot_be_removed_or_deactivate_self(api, db):
    from sqlalchemy import text
    # Make sure the only admin in this transaction is ours.
    db.execute(text("""DELETE FROM user_roles WHERE role_id = (SELECT id FROM roles WHERE code = 'admin')"""))
    admin, tok = admin_token(api, db)
    r = api.patch(f"/api/v1/users/{admin.id}", headers=auth_header(tok), json={"is_active": False})
    assert r.status_code == 409 and r.json()["error"]["code"] == "cannot_deactivate_self"
    r = api.patch(f"/api/v1/users/{admin.id}", headers=auth_header(tok), json={"roles": ["tw_manager"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "last_admin"

    second = make_user(db, roles=["admin"])
    r = api.patch(f"/api/v1/users/{second.id}", headers=auth_header(tok), json={"is_active": False})
    assert r.status_code == 200  # another active admin (ours) remains


def test_admin_reset_password_and_mfa_and_unlock(api, db):
    _, tok = admin_token(api, db)
    u = make_user(db, roles=["read_only"])
    for _ in range(5):
        login(api, u.email, "Wrong-password-123")
    assert login(api, u.email).status_code == 401  # locked
    assert api.post(f"/api/v1/users/{u.id}/unlock", headers=auth_header(tok)).status_code == 204
    assert login(api, u.email).status_code == 200

    r = api.post(f"/api/v1/users/{u.id}/reset-password", headers=auth_header(tok),
                 json={"temporary_password": "Fresh-Temp-Pass-99"})
    assert r.status_code == 204
    assert login(api, u.email, STRONG_PW).status_code == 401
    assert login(api, u.email, "Fresh-Temp-Pass-99").json()["token"]["restrictions"] == ["password_change_required"]
    assert api.post(f"/api/v1/users/{u.id}/reset-mfa", headers=auth_header(tok)).status_code == 204


def test_roles_endpoint_lists_permissions(api, db):
    _, tok = admin_token(api, db)
    roles = {r["code"]: r for r in api.get("/api/v1/roles", headers=auth_header(tok)).json()}
    assert set(roles) == {"admin", "jp_inventory", "us_live_ops", "tw_manager", "read_only"}
    assert "integration.manage" in roles["admin"]["permissions"]


def test_audit_log_filter_and_pagination(api, db):
    admin, tok = admin_token(api, db)
    page = api.get("/api/v1/audit-logs", headers=auth_header(tok),
                   params={"entity_type": "user", "entity_id": str(admin.id), "limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_before_id"] is not None
    nxt = api.get("/api/v1/audit-logs", headers=auth_header(tok),
                  params={"entity_type": "user", "entity_id": str(admin.id), "limit": 2,
                          "before_id": page["next_before_id"]}).json()
    assert all(i["id"] < page["next_before_id"] for i in nxt["items"])


def test_cli_create_admin(db, monkeypatch, capsys):
    import io

    from app import cli
    monkeypatch.setattr(cli, "SessionLocal", lambda: _NoClose(db))
    monkeypatch.setattr("sys.stdin", io.StringIO("Kanagawa-Wave-2026!\n"))
    rc = cli.main(["create-admin", "--email", "owner@example.com", "--name", "Owner", "--password-stdin"])
    assert rc == 0 and "Admin user created" in capsys.readouterr().out
    roles = db.execute(select(AuditLog.after).where(AuditLog.action == "user.created")).scalars().all()
    assert any(r and r.get("email") == "owner@example.com" and r["roles"] == ["admin"] for r in roles)


class _NoClose:
    """Wrap the test session so the CLI's `with SessionLocal()` doesn't close it."""

    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False
