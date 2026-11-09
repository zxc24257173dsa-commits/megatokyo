"""Audit trail. Every write is recorded in the SAME transaction as the change it describes,
so a change can never exist without its audit row (and vice versa)."""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.logging import request_id_var
from app.models.auth import AuditLog

# Never written to the audit log, even if a caller passes them in before/after.
_SECRET_FIELDS = {"password", "password_hash", "mfa_secret_enc", "mfa_pending_secret_enc", "token", "refresh_token",
                  "access_token", "access_token_enc", "refresh_token_enc", "webhook_secret_enc"}


@dataclass(frozen=True)
class RequestContext:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


def _scrub(data: dict | None) -> dict | None:
    if data is None:
        return None
    return {k: ("[redacted]" if k in _SECRET_FIELDS else v) for k, v in data.items()}


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    actor_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
    ctx: RequestContext | None = None,
) -> None:
    ctx = ctx or RequestContext()
    db.add(AuditLog(
        user_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=None if entity_id is None else str(entity_id),
        before=_scrub(before),
        after=_scrub(after),
        ip=ctx.ip,
        user_agent=(ctx.user_agent or "")[:500] or None,
        request_id=ctx.request_id or request_id_var.get(),
    ))
