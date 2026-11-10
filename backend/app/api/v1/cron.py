"""/api/v1/internal/cron/* (Phase 12): called by Cloud Scheduler, not by people.

Protected by a shared secret header (`X-Cron-Secret`, value from Secret Manager). Without
ERP_CRON_SECRET configured these endpoints do not exist (404).

  whatnot-sync   every hour: pending webhooks, then orders / shipments / shows / financial report
  webhooks       every 5 minutes: webhook events not processed yet (Cloud Run may pause work done
                 after a response, so the scheduler makes sure every notification gets handled)
"""
import hmac

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import get_db
from app.integrations.whatnot import client as wn_client
from app.integrations.whatnot import sync, webhooks

router = APIRouter(tags=["internal"], include_in_schema=False)


def _cron(x_cron_secret: str | None = Header(default=None)) -> None:
    secret = get_settings().cron_secret
    if secret is None:
        raise AppError("not_found", "Not found", 404)
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret.encode(), secret.get_secret_value().encode()):
        raise AppError("forbidden", "Forbidden", 403)


@router.post("/internal/cron/whatnot-sync")
def cron_sync(days: int | None = None, _: None = Depends(_cron), db: Session = Depends(get_db)):
    if wn_client.get_token(db) is None:
        return {"skipped": "whatnot not connected"}
    with wn_client.client_for(db) as c:
        pending = webhooks.process_pending(db, c)
        run = sync.run(db, c, days=days)
    return {"webhooks": pending, "sync_run": run.id, "status": run.status,
            "stats": {k: v for k, v in run.stats.items() if k != "errors"}}


@router.post("/internal/cron/webhooks")
def cron_webhooks(_: None = Depends(_cron), db: Session = Depends(get_db)):
    if wn_client.get_token(db) is None:
        return {"skipped": "whatnot not connected"}
    with wn_client.client_for(db) as c:
        return {"webhooks": webhooks.process_pending(db, c)}
