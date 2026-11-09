"""/api/v1/users, /api/v1/roles, /api/v1/audit-logs."""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_request_context, require_permissions
from app.db.session import get_db
from app.models.auth import AuditLog, Permission, Role, User, role_permissions
from app.services import users as svc
from app.services.audit import RequestContext
from app.services.auth import role_codes
from app.schemas.users import (
    AuditLogOut, AuditLogPage, PasswordReset, RoleOut, UserCreate, UserOut, UserUpdate,
)

router = APIRouter(tags=["users"])
manage = require_permissions("users.manage")


def _out(db: Session, u: User) -> UserOut:
    return UserOut(
        id=u.id, email=u.email, display_name=u.display_name, locale=u.locale, timezone=u.timezone,
        is_active=u.is_active, mfa_enabled=u.mfa_enabled, must_change_password=u.must_change_password,
        locked=bool(u.locked_until and u.locked_until > datetime.now(UTC)),
        has_password=u.password_hash is not None, google_linked=u.google_sub is not None,
        last_login_at=u.last_login_at, created_at=u.created_at, roles=role_codes(db, u.id),
    )


@router.get("/users", response_model=list[UserOut])
def list_users(include_inactive: bool = False, _: CurrentUser = Depends(manage), db: Session = Depends(get_db)):
    q = select(User).order_by(User.id)
    if not include_inactive:
        q = q.where(User.is_active)
    return [_out(db, u) for u in db.scalars(q)]


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, current: CurrentUser = Depends(manage), db: Session = Depends(get_db),
                ctx: RequestContext = Depends(get_request_context)):
    user = svc.create_user(
        db, actor_id=current.id, email=body.email, display_name=body.display_name,
        password=body.temporary_password, roles=body.roles, locale=body.locale, timezone=body.timezone, ctx=ctx,
    )
    db.commit()
    return _out(db, user)


@router.get("/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, _: CurrentUser = Depends(manage), db: Session = Depends(get_db)):
    return _out(db, svc.get_user(db, user_id))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, current: CurrentUser = Depends(manage),
                db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    user = svc.update_user(db, actor_id=current.id, user_id=user_id, ctx=ctx, **body.model_dump(exclude_unset=True))
    db.commit()
    return _out(db, user)


@router.post("/users/{user_id}/reset-password", status_code=204)
def reset_password(user_id: int, body: PasswordReset, current: CurrentUser = Depends(manage),
                   db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    svc.admin_reset_password(db, actor_id=current.id, user_id=user_id, temporary_password=body.temporary_password,
                             ctx=ctx)
    db.commit()
    return Response(status_code=204)


@router.post("/users/{user_id}/reset-mfa", status_code=204)
def reset_mfa(user_id: int, current: CurrentUser = Depends(manage), db: Session = Depends(get_db),
              ctx: RequestContext = Depends(get_request_context)):
    svc.admin_reset_mfa(db, actor_id=current.id, user_id=user_id, ctx=ctx)
    db.commit()
    return Response(status_code=204)


@router.post("/users/{user_id}/unlock", status_code=204)
def unlock(user_id: int, current: CurrentUser = Depends(manage), db: Session = Depends(get_db),
           ctx: RequestContext = Depends(get_request_context)):
    svc.admin_unlock(db, actor_id=current.id, user_id=user_id, ctx=ctx)
    db.commit()
    return Response(status_code=204)


@router.get("/roles", response_model=list[RoleOut])
def list_roles(_: CurrentUser = Depends(manage), db: Session = Depends(get_db)):
    out = []
    for role in db.scalars(select(Role).order_by(Role.id)):
        perms = db.scalars(
            select(Permission.code).join(role_permissions, role_permissions.c.permission_id == Permission.id)
            .where(role_permissions.c.role_id == role.id).order_by(Permission.code)
        )
        out.append(RoleOut(code=role.code, name=role.name, description=role.description, permissions=list(perms)))
    return out


@router.get("/audit-logs", response_model=AuditLogPage)
def audit_logs(
    entity_type: str | None = None, entity_id: str | None = None, user_id: int | None = None,
    action: str | None = None, since: datetime | None = None, until: datetime | None = None,
    before_id: int | None = Query(default=None, description="Keyset pagination cursor"),
    limit: int = Query(default=50, ge=1, le=200),
    _: CurrentUser = Depends(require_permissions("audit.view")), db: Session = Depends(get_db),
):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit + 1)
    filters = {
        "entity_type": lambda v: AuditLog.entity_type == v, "entity_id": lambda v: AuditLog.entity_id == v,
        "user_id": lambda v: AuditLog.user_id == v, "action": lambda v: AuditLog.action == v,
        "since": lambda v: AuditLog.created_at >= v, "until": lambda v: AuditLog.created_at < v,
        "before_id": lambda v: AuditLog.id < v,
    }
    values = {"entity_type": entity_type, "entity_id": entity_id, "user_id": user_id, "action": action,
              "since": since, "until": until, "before_id": before_id}
    for name, value in values.items():
        if value is not None:
            q = q.where(filters[name](value))
    rows = list(db.scalars(q))
    more = len(rows) > limit
    rows = rows[:limit]
    return AuditLogPage(
        items=[AuditLogOut.model_validate(r, from_attributes=True) for r in rows],
        next_before_id=rows[-1].id if more else None,
    )
