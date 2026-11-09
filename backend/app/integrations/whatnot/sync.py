"""Reconciliation sync. Webhooks are notifications, not a guaranteed log (Whatnot's own advice),
so a periodic sync re-reads recent orders / shipments / shows and anything still open.

Each record is processed in its own savepoint and committed, so one bad record can't block the
rest; failures are counted in sync_runs.stats. A Postgres advisory lock prevents two syncs
from running at once.
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.whatnot import processing as P
from app.integrations.whatnot.client import WhatnotClient
from app.models.integration import SyncRun
from app.models.sales import SHIPPED_STATUSES, SalesOrder, SalesOrderLine, SalesShipment

log = logging.getLogger("app.whatnot.sync")
SYNC_LOCK = 7_420_105


def _each(db: Session, stats: dict, key: str, fn, *args) -> None:
    try:
        with db.begin_nested():
            fn(*args)
        db.commit()
        stats[key] = stats.get(key, 0) + 1
    except Exception as exc:  # keep going; the next sync retries it
        db.rollback()
        stats["failed"] = stats.get("failed", 0) + 1
        stats.setdefault("errors", []).append(f"{key}: {exc}"[:300])
        log.exception("whatnot sync item failed")


def sync_orders(db: Session, client: WhatnotClient, cutoff: datetime, stats: dict) -> None:
    seen = set()
    for order in client.paginate("/orders", limit=50, reverse=True):
        created = P.parse_dt(order.get("created_at"))
        if created and created < cutoff:
            break
        seen.add(order["id"])
        _each(db, stats, "orders", lambda o=order: P.process_order(db, o, client.order_items(o["id"]), source="api"))
    # older orders that are still open (not paid yet, or paid but not shipped)
    open_ids = list(db.scalars(
        select(SalesOrder.external_order_id).where(
            SalesOrder.platform == "whatnot", SalesOrder.status.in_(("pending", "pending_fulfillment")),
            SalesOrder.ordered_at < cutoff).order_by(SalesOrder.ordered_at.desc()).limit(500)))
    for oid in open_ids:
        if oid not in seen:
            _each(db, stats, "open_orders", lambda i=oid: P.process_order(db, client.order(i), client.order_items(i),
                                                                          source="api"))


def sync_shipments(db: Session, client: WhatnotClient, cutoff: datetime, stats: dict) -> None:
    seen = set()
    for sh in client.paginate("/shipments", limit=50, reverse=True):
        created = P.parse_dt(sh.get("created_at"))
        if created and created < cutoff:
            break
        seen.add(sh["id"])
        _each(db, stats, "shipments", lambda s=sh: P.process_shipment(db, s, client.shipment_items(s["id"]), source="api"))
    # reserved lines whose shipment we know but which hasn't shipped yet
    pending = list(db.scalars(
        select(SalesOrderLine.external_shipment_id).distinct().where(
            SalesOrderLine.stock_state == "reserved", SalesOrderLine.external_shipment_id.is_not(None))
        .limit(500)))
    for sid in pending:
        known = db.scalar(select(SalesShipment.status).where(SalesShipment.external_id == sid))
        if sid not in seen and known not in SHIPPED_STATUSES:
            _each(db, stats, "open_shipments", lambda i=sid: P.process_shipment(db, client.shipment(i),
                                                                                client.shipment_items(i), source="api"))


def sync_shows(db: Session, client: WhatnotClient, stats: dict, max_shows: int = 100) -> None:
    for n, show in enumerate(client.paginate("/shows", limit=50, reverse=True)):
        if n >= max_shows:
            break
        _each(db, stats, "shows", lambda s=show: P.upsert_show(db, s, "api"))


def retry_exceptions(db: Session, stats: dict) -> None:
    """Lines waiting on stock / mapping get another chance (e.g. after a receipt was posted)."""
    ids = list(db.scalars(select(SalesOrderLine.order_id).distinct()
                          .where(SalesOrderLine.exception_code.in_(("insufficient_stock", "unmapped_sku"))).limit(500)))
    for oid in ids:
        _each(db, stats, "retried", lambda i=oid: P.reprocess_order(db, db.get(SalesOrder, i), "system"))


def run(db: Session, client: WhatnotClient, *, days: int | None = None, kinds=("shows", "orders", "shipments")) -> SyncRun:
    days = days or get_settings().whatnot_sync_days
    if not db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SYNC_LOCK}).scalar():
        run = SyncRun(platform="whatnot", kind="+".join(kinds), status="failed", error="another sync is running",
                      finished_at=datetime.now(UTC))
        db.add(run)
        db.commit()
        return run
    now = datetime.now(UTC)
    run = SyncRun(platform="whatnot", kind="+".join(kinds), window_start=now - timedelta(days=days), window_end=now)
    db.add(run)
    db.commit()
    stats: dict = {}
    try:
        if "shows" in kinds:
            sync_shows(db, client, stats)
        if "orders" in kinds:
            sync_orders(db, client, run.window_start, stats)
        if "shipments" in kinds:
            sync_shipments(db, client, run.window_start, stats)
        retry_exceptions(db, stats)
        run.status = "succeeded" if not stats.get("failed") else "failed"
    except Exception as exc:
        db.rollback()
        run = db.get(SyncRun, run.id)
        run.status, run.error = "failed", str(exc)[:1000]
        log.exception("whatnot sync failed")
    finally:
        stats["errors"] = stats.get("errors", [])[:20]
        run.stats, run.finished_at = stats, datetime.now(UTC)
        db.commit()
        db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SYNC_LOCK})
        db.commit()
    return run


def open_exception_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(SalesOrderLine).where(SalesOrderLine.exception_code.is_not(None)))
