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
from app.integrations.whatnot import financial
from app.integrations.whatnot import processing as P
from app.integrations.whatnot.client import WhatnotClient, WhatnotError
from app.models.integration import SyncRun
from app.models.sales import SHIPPED_STATUSES, Livestream, LivestreamMetric, SalesOrder, SalesOrderLine, SalesShipment

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


def refresh_show_details(db: Session, client: WhatnotClient, stats: dict) -> None:
    """Shows first seen through an order (only the id is known) or not finished yet: fetch them by id."""
    ids = list(db.scalars(select(Livestream.external_id).where(
        Livestream.platform == "whatnot", Livestream.external_id.is_not(None),
        (Livestream.title.is_(None)) | (Livestream.status.in_(("scheduled", "live", "stopped")))).limit(200)))
    for sid in ids:
        _each(db, stats, "show_details", lambda i=sid: P.upsert_show(db, client.show(i), "api"))


ANALYTICS_COUNTS = ("orders_count", "giveaway_orders_count", "show_impression_count", "buyers_count",
                    "first_time_buyers_count", "returning_buyers_count", "giveaway_winners_count",
                    "first_time_giveaway_winners_count", "returning_giveaway_winners_count", "buyer_shares_count",
                    "stream_minutes", "max_concurrent_viewers", "total_views", "distinct_viewers_count")
ANALYTICS_MONEY = ("sales", "estimated_earnings", "average_order_value", "giveaway_spend")
ANALYTICS_RATIOS = ("show_clickthrough_rate", "average_rating")


def store_show_analytics(db: Session, ls: Livestream, a: dict) -> None:
    """Whatnot's own numbers for a show. Only stored once final (GENERATED); shown next to the
    ERP's figures, never mixed into them."""
    P.record_raw(db, "show_analytics", ls.external_id, a, "api")
    ls.analytics_status, ls.analytics_fetched_at = a.get("calculation_status"), datetime.now(UTC)
    if ls.started_at is None and a.get("start_time"):
        ls.started_at = P.parse_dt(a["start_time"])
    if a.get("calculation_status") != "GENERATED":
        db.flush()
        return
    values = {k: a.get(k) for k in ANALYTICS_COUNTS + ANALYTICS_RATIOS}
    for k in ANALYTICS_MONEY:
        m = a.get(k)
        if m and (m.get("currency") or "USD") == "USD":
            values[k] = m.get("amount")
    for code, v in values.items():
        if v is None:
            continue
        row = db.scalar(select(LivestreamMetric).where(LivestreamMetric.livestream_id == ls.id,
                                                       LivestreamMetric.metric_code == code,
                                                       LivestreamMetric.source == "api"))
        if row is None:
            db.add(LivestreamMetric(livestream_id=ls.id, metric_code=code, value=v, source="api"))
        else:
            row.value = v
    db.flush()


def refresh_show_analytics(db: Session, client: WhatnotClient, stats: dict, max_shows: int = 50) -> None:
    since = datetime.now(UTC) - timedelta(days=60)
    shows = list(db.scalars(select(Livestream).where(
        Livestream.platform == "whatnot", Livestream.external_id.is_not(None),
        Livestream.status.in_(("ended", "stopped")),
        (Livestream.analytics_status.is_(None)) | (Livestream.analytics_status.in_(("PENDING", "NOT_ENDED"))),
        func.coalesce(Livestream.ended_at, Livestream.scheduled_at, Livestream.created_at) >= since)
        .order_by(Livestream.id.desc()).limit(max_shows)))
    for ls in shows:
        try:
            a = client.show_analytics(ls.external_id)
        except WhatnotError as e:
            if e.status != 404:
                stats["failed"] = stats.get("failed", 0) + 1
                stats.setdefault("errors", []).append(f"show_analytics: {e.message}"[:300])
                continue
            # not this seller's show / no analytics: remember, don't ask again
            ls.analytics_status, ls.analytics_fetched_at = "UNAVAILABLE", datetime.now(UTC)
            db.commit()
            continue
        _each(db, stats, "show_analytics", lambda s=ls, a=a: store_show_analytics(db, s, a))


def retry_exceptions(db: Session, stats: dict) -> None:
    """Lines waiting on stock / mapping get another chance (e.g. after a receipt was posted)."""
    ids = list(db.scalars(select(SalesOrderLine.order_id).distinct()
                          .where(SalesOrderLine.exception_code.in_(("insufficient_stock", "unmapped_sku"))).limit(500)))
    for oid in ids:
        _each(db, stats, "retried", lambda i=oid: P.reprocess_order(db, db.get(SalesOrder, i), "system"))


def run(db: Session, client: WhatnotClient, *, days: int | None = None,
        kinds=("shows", "orders", "shipments", "financial"), financial_kw: dict | None = None) -> SyncRun:
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
        if "shows" in kinds:
            refresh_show_details(db, client, stats)
            refresh_show_analytics(db, client, stats)
        retry_exceptions(db, stats)
        if "financial" in kinds:  # after orders, so fees find their orders
            financial.sync(db, client, stats, max(days, get_settings().whatnot_financial_days),
                           **(financial_kw or {}))
        run.status = "succeeded" if not stats.get("failed") and not stats.get("financial", {}).get("failed_runs") \
            else "failed"
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
