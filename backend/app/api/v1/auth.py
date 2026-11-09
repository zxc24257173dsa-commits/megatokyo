"""/api/v1/auth - login, MFA, refresh, logout, password change, current user.

Access token: short-lived JWT returned in the body (frontend keeps it in memory only).
Refresh token: opaque, httpOnly + Secure + SameSite=Strict cookie scoped to /api/v1/auth,
so page scripts can never read it.
"""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_request_context
from app.core.config import get_settings
from app.db.session import get_db
from app.services import auth as svc
from app.services.audit import RequestContext
from app.schemas.auth import (
    LoginRequest, LoginResponse, MeResponse, MfaCode, MfaDisableRequest, MfaSetupResponse, MfaVerifyRequest,
    PasswordChangeRequest, TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])
COOKIE_PATH = "/api/v1/auth"


def _set_refresh_cookie(response: Response, raw: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.refresh_cookie_name, raw, max_age=s.refresh_token_ttl_days * 86400, path=COOKIE_PATH,
        httponly=True, secure=s.cookie_secure, samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(s.refresh_cookie_name, path=COOKIE_PATH, httponly=True, secure=s.cookie_secure,
                           samesite="strict")


def _token(session: svc.IssuedSession, response: Response) -> TokenResponse:
    _set_refresh_cookie(response, session.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=session.access_token, expires_in=session.expires_in,
                         restrictions=session.restrictions)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db),
          ctx: RequestContext = Depends(get_request_context)):
    result = svc.login(db, body.email, body.password, ctx)
    if result.mfa_token:
        return LoginResponse(mfa_required=True, mfa_token=result.mfa_token)
    db.commit()
    return LoginResponse(token=_token(result.session, response))


@router.post("/mfa/verify", response_model=TokenResponse)
def mfa_verify(body: MfaVerifyRequest, response: Response, db: Session = Depends(get_db),
               ctx: RequestContext = Depends(get_request_context)):
    session = svc.verify_mfa_login(db, body.mfa_token, body.code, ctx)
    db.commit()
    return _token(session, response)


@router.post("/refresh", response_model=TokenResponse)
def refresh(request: Request, response: Response, db: Session = Depends(get_db),
            ctx: RequestContext = Depends(get_request_context)):
    session = svc.refresh(db, request.cookies.get(get_settings().refresh_cookie_name), ctx)
    db.commit()
    return _token(session, response)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db),
           ctx: RequestContext = Depends(get_request_context)):
    svc.logout(db, request.cookies.get(get_settings().refresh_cookie_name), ctx)
    db.commit()
    _clear_refresh_cookie(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=MeResponse)
def me(current: CurrentUser = Depends(get_current_user)):
    u = current.user
    return MeResponse(
        id=u.id, email=u.email, display_name=u.display_name, locale=u.locale, timezone=u.timezone,
        mfa_enabled=u.mfa_enabled, last_login_at=u.last_login_at, roles=current.roles,
        permissions=sorted(current.permissions), restrictions=current.restrictions,
    )


@router.post("/password", response_model=TokenResponse)
def change_password(body: PasswordChangeRequest, response: Response, current: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    session = svc.change_password(db, current.user, body.current_password, body.new_password, ctx)
    db.commit()
    return _token(session, response)


@router.post("/mfa/setup", response_model=MfaSetupResponse)
def mfa_setup(response: Response, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
              ctx: RequestContext = Depends(get_request_context)):
    secret, uri = svc.mfa_begin_setup(db, current.user, ctx)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return MfaSetupResponse(secret=secret, otpauth_uri=uri)


@router.post("/mfa/enable", status_code=204)
def mfa_enable(body: MfaCode, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
               ctx: RequestContext = Depends(get_request_context)):
    svc.mfa_enable(db, current.user, body.code, ctx)
    db.commit()
    return Response(status_code=204)


@router.post("/mfa/disable", status_code=204)
def mfa_disable(body: MfaDisableRequest, current: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    svc.mfa_disable(db, current.user, body.password, body.code, ctx)
    db.commit()
    return Response(status_code=204)
