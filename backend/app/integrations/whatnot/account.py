"""Whatnot connection settings stored in integration_accounts.

The API token and webhook secret are encrypted with AES-256-GCM (app.core.crypto). They are
write-only from the API's point of view: the admin screen can replace them, never read them.
"""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import get_settings
from app.core.errors import AppError
from app.models.integration import IntegrationAccount
from app.services import audit
from app.services.audit import RequestContext

PLATFORM, ENV = "whatnot", "production"
TOKEN_CTX = "integration:whatnot:production:access_token"
SECRET_CTX = "integration:whatnot:production:webhook_secret"


def get_account(db: Session, *, for_update: bool = False) -> IntegrationAccount | None:
    q = select(IntegrationAccount).where(IntegrationAccount.platform == PLATFORM, IntegrationAccount.environment == ENV)
    if for_update:
        q = q.with_for_update().execution_options(populate_existing=True)
    return db.scalar(q)


def stored_token(db: Session) -> str | None:
    acc = get_account(db)
    if acc is None or acc.access_token_enc is None or acc.status == "revoked":
        return None
    return crypto.decrypt(acc.access_token_enc, context=TOKEN_CTX)


def webhook_secret(db: Session) -> str | None:
    s = get_settings()
    if s.whatnot_webhook_secret:
        return s.whatnot_webhook_secret.get_secret_value()
    acc = get_account(db)
    if acc is None or acc.webhook_secret_enc is None:
        return None
    return crypto.decrypt(acc.webhook_secret_enc, context=SECRET_CTX)


def save_credentials(db: Session, *, actor_id: int, token: str | None, webhook_secret_value: str | None,
                     ctx: RequestContext) -> IntegrationAccount:
    if token is not None and not token.startswith(("wn_pat_tk_", "wn_access_tk_")):
        raise AppError("invalid_whatnot_token", "A Whatnot token starts with wn_pat_tk_ (personal access token)", 422)
    if webhook_secret_value is not None and not webhook_secret_value.startswith("wn_wh_"):
        raise AppError("invalid_webhook_secret", "A Whatnot webhook secret starts with wn_wh_", 422)
    acc = get_account(db, for_update=True)
    if acc is None:
        acc = IntegrationAccount(platform=PLATFORM, environment=ENV, status="inactive")
        db.add(acc)
    changed = []
    if token is not None:
        acc.access_token_enc = crypto.encrypt(token, context=TOKEN_CTX)
        acc.status = "inactive"  # becomes active after a successful connection test
        changed.append("access_token")
    if webhook_secret_value is not None:
        acc.webhook_secret_enc = crypto.encrypt(webhook_secret_value, context=SECRET_CTX)
        changed.append("webhook_secret")
    db.flush()
    audit.record(db, action="integration.credentials_saved", entity_type="integration", entity_id=acc.id,
                 actor_id=actor_id, after={"platform": PLATFORM, "changed": changed}, ctx=ctx)
    return acc


def mark_connected(db: Session, *, me: dict) -> None:
    acc = get_account(db, for_update=True)
    if acc is None:
        acc = IntegrationAccount(platform=PLATFORM, environment=ENV)
        db.add(acc)
    acc.seller_id = str(me.get("id"))
    acc.display_name = me.get("username") or me.get("display_name")
    acc.status = "active"
    acc.updated_at = datetime.now(UTC)
    db.flush()
