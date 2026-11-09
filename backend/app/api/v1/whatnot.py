"""/api/v1/integrations/whatnot (admin), /api/v1/webhooks/whatnot (public, signed),
/api/v1/orders (order list / detail / exceptions / manual SKU mapping)."""
import logging
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_request_context, require_permissions
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import SessionLocal, get_db
from app.integrations.whatnot import account, client as wn_client, processing, sync, webhooks
from app.models.catalog import ExternalSkuMapping, Sku
from app.models.integration import SyncRun, WebhookEvent
from app.models.sales import SalesOrder, SalesOrderLine
from app.services import audit
from app.services.audit import RequestContext

router = APIRouter(tags=["whatnot"])
log = logging.getLogger("app.whatnot.api")
manage = require_permissions("integration.manage")


# ---------- background jobs (own DB session) ----------

def _run_sync_job(days: int | None) -> None:
    with SessionLocal() as db:
        try:
            with wn_client.client_for(db) as c:
                webhooks.process_pending(db, c)
                sync.run(db, c, days=days)
        except AppError as e:
            log.warning("sync not started: %s", e.message)


def _process_event_job(event_id: int) -> None:
    with SessionLocal() as db:
        try:
            with wn_client.client_for(db) as c:
                webhooks.process_event(db, c, event_id)
        except AppError as e:
            log.warning("webhook %s left pending: %s", event_id, e.message)


# ---------- integration settings ----------

@router.get("/integrations/whatnot")
def status(_: CurrentUser = Depends(manage), db: Session = Depends(get_db)):
    s = get_settings()
    acc = account.get_account(db)
    runs = db.scalars(select(SyncRun).where(SyncRun.platform == "whatnot").order_by(SyncRun.id.desc()).limit(10))
    counts = dict(db.execute(select(WebhookEvent.status, func.count()).where(WebhookEvent.platform == "whatnot")
                             .group_by(WebhookEvent.status)).all())
    return {
        "token_source": "environment" if s.whatnot_api_token else ("saved" if acc and acc.access_token_enc else None),
        "webhook_secret_configured": bool(account.webhook_secret(db)),
        "connected": bool(acc and acc.status == "active"),
        "seller": {"id": acc.seller_id, "name": acc.display_name} if acc and acc.seller_id else None,
        "webhook_url": s.public_base_url.rstrip("/") + "/api/v1/webhooks/whatnot",
        "webhook_events": counts,
        "open_exceptions": sync.open_exception_count(db),
        "sync_runs": [{"id": r.id, "kind": r.kind, "status": r.status, "started_at": r.started_at,
                       "finished_at": r.finished_at, "stats": r.stats, "error": r.error} for r in runs],
    }


class CredentialsIn(BaseModel):
    token: str | None = Field(default=None, min_length=10, max_length=500)
    webhook_secret: str | None = Field(default=None, min_length=10, max_length=500)


@router.put("/integrations/whatnot/credentials", status_code=204)
def save_credentials(body: CredentialsIn, current: CurrentUser = Depends(manage), db: Session = Depends(get_db),
                     ctx: RequestContext = Depends(get_request_context)):
    account.save_credentials(db, actor_id=current.id, token=body.token, webhook_secret_value=body.webhook_secret, ctx=ctx)
    db.commit()
    return Response(status_code=204)


@router.post("/integrations/whatnot/test")
def test_connection(current: CurrentUser = Depends(manage), db: Session = Depends(get_db),
                    ctx: RequestContext = Depends(get_request_context)):
    with wn_client.client_for(db) as c:
        me = c.me()
    account.mark_connected(db, me=me)
    audit.record(db, action="integration.tested", entity_type="integration", actor_id=current.id,
                 after={"seller_id": str(me.get("id")), "username": me.get("username")}, ctx=ctx)
    db.commit()
    return {"ok": True, "seller": {"id": str(me.get("id")), "name": me.get("username") or me.get("display_name"),
                                   "currency": me.get("seller_currency")}}


class SyncIn(BaseModel):
    days: int | None = Field(default=None, ge=1, le=90)


@router.post("/integrations/whatnot/sync", status_code=202)
def start_sync(body: SyncIn, tasks: BackgroundTasks, current: CurrentUser = Depends(manage),
               db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    if not wn_client.get_token(db):
        raise AppError("whatnot_not_connected", "Whatnot API token is not configured", 409)
    audit.record(db, action="integration.sync_requested", entity_type="integration", actor_id=current.id,
                 after={"days": body.days}, ctx=ctx)
    db.commit()
    tasks.add_task(_run_sync_job, body.days)
    return {"started": True}


# ---------- webhook receiver (public; authenticated by signature) ----------

@router.post("/webhooks/whatnot", include_in_schema=False)
async def whatnot_webhook(request: Request, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    outcome, event_id = webhooks.receive(db, headers, body)
    if outcome == "invalid_signature":
        raise AppError("invalid_signature", "Invalid webhook signature", 401)
    if outcome in ("not_configured", "bad_payload"):
        raise AppError(outcome, "Webhook not accepted", 400)
    if event_id:
        tasks.add_task(_process_event_job, event_id)  # answer Whatnot within its 5 s timeout, work afterwards
    return {"received": True, "duplicate": outcome == "duplicate"}


# ---------- orders ----------

orders_view = require_permissions("orders.view")


@router.get("/orders")
def list_orders(status: str | None = None, exceptions_only: bool = False, livestream_id: int | None = None,
                search: str | None = Query(default=None, max_length=100), limit: int = Query(default=100, le=500),
                _: CurrentUser = Depends(orders_view), db: Session = Depends(get_db)):
    where, params = ["TRUE"], {"limit": limit}
    if status:
        where.append("o.status = :status")
        params["status"] = status
    if exceptions_only:
        where.append("EXISTS (SELECT 1 FROM sales_order_lines x WHERE x.order_id = o.id AND x.exception_code IS NOT NULL)")
    if livestream_id:
        where.append("o.livestream_id = :ls")
        params["ls"] = livestream_id
    if search:
        where.append("(o.external_order_id ILIKE :q OR EXISTS (SELECT 1 FROM sales_order_lines x LEFT JOIN skus s ON s.id = x.sku_id "
                     "WHERE x.order_id = o.id AND (x.title_snapshot ILIKE :q OR s.sku_code ILIKE :q)))")
        params["q"] = f"%{search}%"
    rows = db.execute(text(f"""
        SELECT o.id, o.external_order_id, o.status, o.ordered_at, o.paid_at, o.currency, o.item_subtotal, o.total,
               o.transaction_type, l.title AS livestream_title, l.id AS livestream_id,
               (SELECT count(*) FROM sales_order_lines x WHERE x.order_id = o.id) AS line_count,
               (SELECT sum(x.qty) FROM sales_order_lines x WHERE x.order_id = o.id) AS units,
               (SELECT count(*) FROM sales_order_lines x WHERE x.order_id = o.id AND x.exception_code IS NOT NULL) AS exceptions,
               (SELECT string_agg(DISTINCT x.stock_state, ',') FROM sales_order_lines x WHERE x.order_id = o.id) AS stock_states
          FROM sales_orders o LEFT JOIN livestreams l ON l.id = o.livestream_id
         WHERE {' AND '.join(where)} ORDER BY o.ordered_at DESC LIMIT :limit"""), params).mappings().all()
    return [dict(r) for r in rows]


@router.get("/orders/unmapped-products")
def unmapped_products(_: CurrentUser = Depends(orders_view), db: Session = Depends(get_db)):
    """Lines without a SKU, grouped per product (auction titles without their #N), most sold first."""
    groups: dict[str, dict] = {}
    for line in db.scalars(select(SalesOrderLine).where(SalesOrderLine.sku_id.is_(None)).order_by(SalesOrderLine.id)):
        key = processing.title_key(line.title_snapshot) or f"line:{line.id}"
        g = groups.setdefault(key, {"title": processing.clean_title(line.title_snapshot) or "（沒有名稱）",
                                    "example_line_id": line.id, "lines": 0, "units": 0, "sales": Decimal(0)})
        g["lines"] += 1
        g["units"] += line.qty
        g["sales"] += line.line_amount
    return sorted(groups.values(), key=lambda g: (-g["units"], g["title"]))


@router.get("/orders/{order_id}")
def get_order(order_id: int, _: CurrentUser = Depends(orders_view), db: Session = Depends(get_db)):
    so = db.get(SalesOrder, order_id)
    if so is None:
        raise AppError("not_found", "Order not found", 404)
    lines = db.execute(text("""
        SELECT x.id, x.external_line_id, x.title_snapshot, x.purchased_sku, x.qty, x.unit_price, x.line_amount,
               x.stock_state, x.exception_code, x.exception_detail, x.external_shipment_id, x.shipped_at,
               x.sku_id, s.sku_code, s.name AS sku_name, s.is_bundle, sh.status AS shipment_status,
               sh.tracking_code,
               (SELECT sum(c.total_cost_base) FROM cogs_allocations c WHERE c.order_line_id = x.id) AS cogs
          FROM sales_order_lines x LEFT JOIN skus s ON s.id = x.sku_id
          LEFT JOIN sales_shipments sh ON sh.external_id = x.external_shipment_id
         WHERE x.order_id = :o ORDER BY x.id"""), {"o": so.id}).mappings().all()
    ls = so.livestream_id and db.execute(text("SELECT id, title, external_id FROM livestreams WHERE id = :i"),
                                         {"i": so.livestream_id}).mappings().first()
    return {"id": so.id, "external_order_id": so.external_order_id, "status": so.status, "ordered_at": so.ordered_at,
            "paid_at": so.paid_at, "cancelled_at": so.cancelled_at, "currency": so.currency,
            "item_subtotal": so.item_subtotal, "shipping_charged": so.shipping_charged,
            "sales_tax_collected": so.sales_tax_collected, "discount_total": so.discount_total, "total": so.total,
            "transaction_type": so.transaction_type, "source": so.source, "last_synced_at": so.last_synced_at,
            "livestream": dict(ls) if ls else None, "lines": [dict(l) for l in lines]}


class MapIn(BaseModel):
    sku_id: int


@router.post("/orders/lines/{line_id}/map")
def map_line(line_id: int, body: MapIn, current: CurrentUser = Depends(require_permissions("catalog.edit")),
             db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    """Link a Whatnot product to an ERP SKU. Applies to this line and every future order of it."""
    line = db.scalar(select(SalesOrderLine).where(SalesOrderLine.id == line_id).with_for_update())
    if line is None:
        raise AppError("not_found", "Order line not found", 404)
    if line.sku_id is not None:
        raise AppError("already_mapped", "This line is already linked to a SKU", 409)
    sku = db.get(Sku, body.sku_id)
    if sku is None:
        raise AppError("not_found", "SKU not found", 404)
    if line.external_product_id:
        exists = db.scalar(select(ExternalSkuMapping).where(
            ExternalSkuMapping.platform == "whatnot", ExternalSkuMapping.external_product_id == line.external_product_id,
            ExternalSkuMapping.external_variant_id.is_(None) if line.external_variant_id is None
            else ExternalSkuMapping.external_variant_id == line.external_variant_id))
        if exists is None:
            db.add(ExternalSkuMapping(platform="whatnot", external_product_id=line.external_product_id,
                                      external_variant_id=line.external_variant_id, external_sku=line.purchased_sku,
                                      sku_id=sku.id, created_by=current.id))
    line.sku_id, line.mapping_status = sku.id, "mapped"
    db.flush()
    audit.record(db, action="order_line.mapped", entity_type="sales_order_line", entity_id=line.id, actor_id=current.id,
                 after={"sku_code": sku.sku_code, "whatnot_product_id": line.external_product_id}, ctx=ctx)
    processing.save_title_rule(db, line.title_snapshot, sku.id, current.id)
    so = db.get(SalesOrder, line.order_id)
    processing.reprocess_order(db, so, "manual")
    # every other unmapped line of the same Whatnot product, or with the same title (any #N), too
    key = processing.title_key(line.title_snapshot)
    for other in db.scalars(select(SalesOrderLine).where(SalesOrderLine.sku_id.is_(None))):
        same_product = line.external_product_id and other.external_product_id == line.external_product_id \
            and other.external_variant_id == line.external_variant_id
        if same_product or (key and processing.title_key(other.title_snapshot) == key):
            other.sku_id, other.mapping_status = sku.id, "mapped"
            db.flush()
            processing.reprocess_order(db, db.get(SalesOrder, other.order_id), "manual")
    db.commit()
    return get_order(so.id, current, db)


@router.post("/orders/{order_id}/reprocess")
def reprocess(order_id: int, current: CurrentUser = Depends(require_permissions("orders.view", "inventory.adjust_us")),
              db: Session = Depends(get_db)):
    so = db.get(SalesOrder, order_id)
    if so is None:
        raise AppError("not_found", "Order not found", 404)
    processing.reprocess_order(db, so, "manual")
    db.commit()
    return get_order(order_id, current, db)
