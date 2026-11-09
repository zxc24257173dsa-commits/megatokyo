"""Turn Whatnot orders / shipments / shows into ERP records. Every step is idempotent, so the
same order arriving from a webhook, a retry and a sync produces exactly one result.

Order status (Whatnot)          ERP effect
  pending                       recorded only (not paid / not ready to fulfil)
  pending_fulfillment, completed  PAID: revenue booked once per item; US stock reserved
  cancelled, failed             reservation released; revenue (and coupon) reversed;
                                if goods had already shipped -> exception for manual handling
Shipment status dropped_off / in_transit / delivered, or order completed:
                                SHIPPED: reserved units leave US stock FIFO; COGS booked per lot

Problems that need a person (unknown SKU, not enough stock to ship, non-USD order, cancelled
after shipping) are written to the line's exception fields, never guessed around.
Notes: Whatnot does not expose the payment time, so paid_at = the order's created_at.
Sales tax and buyer-paid shipping are not revenue. Fees / refunds come from the financial
report (Phase 5b/6), not from guessed rates.
"""
import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.catalog import ExternalSkuMapping, Sku, sku_bundle_components
from app.models.finance import FinancialEntry
from app.models.integration import ExternalRawRecord
from app.models.inventory import InventoryReservation
from app.models.sales import SHIPPED_STATUSES, CogsAllocation, Livestream, SalesOrder, SalesOrderLine, SalesShipment
from app.services import inventory

PLATFORM = "whatnot"
PAID = ("pending_fulfillment", "completed")
DEAD = ("cancelled", "failed")
SHOW_STATUSES = ("scheduled", "live", "stopped", "ended", "cancelled")


# ---------- small helpers ----------

def parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def money(m: dict | None) -> Decimal | None:
    return Decimal(str(m["amount"])) if m and m.get("amount") is not None else None


def biz_date(dt: datetime) -> date:
    return dt.astimezone(ZoneInfo(get_settings().business_timezone)).date()


def q4(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def record_raw(db: Session, entity_type: str, external_id: str, payload: dict, source: str) -> None:
    """Keep every distinct version of what Whatnot sent (traceability)."""
    body = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()
    db.execute(text("""INSERT INTO external_raw_records (platform, entity_type, external_id, payload_hash, payload, source)
                       VALUES (:p, :t, :e, :h, CAST(:body AS jsonb), :s)
                       ON CONFLICT (platform, entity_type, external_id, payload_hash) DO NOTHING"""),
               {"p": PLATFORM, "t": entity_type, "e": external_id, "h": digest, "body": body,
                "s": "webhook" if source == "webhook" else "api" if source == "api" else "csv"})


def _entry(db: Session, key: str, **fields) -> FinancialEntry:
    existing = db.scalar(select(FinancialEntry).where(FinancialEntry.idempotency_key == key))
    if existing:
        return existing
    e = FinancialEntry(idempotency_key=key, fx_rate=Decimal(1), currency_orig="USD", **fields)
    e.amount_base = e.amount_orig
    db.add(e)
    db.flush()
    return e


def _reverse(db: Session, key: str, when: datetime, source_type: str) -> None:
    orig = db.scalar(select(FinancialEntry).where(FinancialEntry.idempotency_key == key))
    if orig is None or db.scalar(select(FinancialEntry.id).where(FinancialEntry.reversal_of_id == orig.id)):
        return
    db.add(FinancialEntry(
        idempotency_key=f"{key}:reversal", entry_date=biz_date(when), occurred_at=when, entry_type=orig.entry_type,
        amount_orig=-orig.amount_orig, currency_orig=orig.currency_orig, fx_rate=orig.fx_rate,
        amount_base=-orig.amount_base, sales_order_id=orig.sales_order_id, order_line_id=orig.order_line_id,
        livestream_id=orig.livestream_id, sku_id=orig.sku_id, source_type=source_type, source_ref=orig.source_ref,
        reversal_of_id=orig.id, memo="order cancelled"))
    db.flush()


def _set_exception(line: SalesOrderLine, code: str | None, detail: str | None = None) -> None:
    line.exception_code, line.exception_detail = code, detail


# ---------- shows ----------

def livestream_for(db: Session, show_id: str | None, source: str) -> Livestream | None:
    if not show_id:
        return None
    ls = db.scalar(select(Livestream).where(Livestream.platform == PLATFORM, Livestream.external_id == show_id))
    if ls is None:
        ls = Livestream(platform=PLATFORM, external_id=show_id, status="scheduled", source="api" if source != "csv" else "csv")
        db.add(ls)
        db.flush()
    return ls


def upsert_show(db: Session, show: dict, source: str) -> Livestream:
    record_raw(db, "show", show["id"], show, source)
    ls = livestream_for(db, show["id"], source)
    ls.title = show.get("title")
    ls.scheduled_at = parse_dt(show.get("scheduled_start_at"))
    ls.ended_at = parse_dt(show.get("ended_at"))
    status = show.get("status")
    if status in SHOW_STATUSES:
        if status == "live" and ls.started_at is None:
            ls.started_at = datetime.now(UTC)
        ls.status = status
    db.flush()
    return ls


# ---------- SKU resolution ----------

TITLE_PLATFORM = "whatnot_title"  # mapping rule: listing title without its "#N" auction number


def title_key(title: str | None) -> str | None:
    """'1x PERONA OP14-111 PROMO ... #12' -> stable key shared by every auction of that product."""
    if not title:
        return None
    t = re.sub(r"\s*#\d+\s*$", "", title.strip())
    t = re.sub(r"\s+", " ", t).upper()
    return "t:" + hashlib.sha256(t.encode()).hexdigest()[:40] if t else None


def clean_title(title: str | None) -> str | None:
    return re.sub(r"\s*#\d+\s*$", "", title.strip())[:128] if title else None


def save_title_rule(db: Session, title: str | None, sku_id: int, actor_id: int | None = None) -> None:
    key = title_key(title)
    if key and not db.scalar(select(ExternalSkuMapping.id).where(ExternalSkuMapping.platform == TITLE_PLATFORM,
                                                                 ExternalSkuMapping.external_product_id == key)):
        db.add(ExternalSkuMapping(platform=TITLE_PLATFORM, external_product_id=key, external_sku=clean_title(title),
                                  sku_id=sku_id, created_by=actor_id))
        db.flush()

def resolve_sku(db: Session, item: dict) -> int | None:
    pid, vid = item.get("product_id"), item.get("variant_id")
    if pid:
        for v in (vid, None) if vid else (None,):
            m = db.scalar(select(ExternalSkuMapping.sku_id).where(
                ExternalSkuMapping.platform == PLATFORM, ExternalSkuMapping.external_product_id == pid,
                ExternalSkuMapping.external_variant_id.is_(None) if v is None
                else ExternalSkuMapping.external_variant_id == v))
            if m:
                return m
    purchased = (item.get("purchased_item") or {}).get("sku")
    for code in (item.get("variant_external_id"), item.get("product_external_id"), purchased):
        if not code:
            continue
        sid = db.scalar(select(Sku.id).where(func.upper(Sku.sku_code) == code.strip().upper()))
        if sid:
            if pid:  # remember it, so a later rename on Whatnot keeps mapping
                exists = db.scalar(select(ExternalSkuMapping.id).where(
                    ExternalSkuMapping.platform == PLATFORM, ExternalSkuMapping.external_product_id == pid,
                    ExternalSkuMapping.external_variant_id.is_(None) if vid is None
                    else ExternalSkuMapping.external_variant_id == vid))
                if not exists:
                    db.add(ExternalSkuMapping(platform=PLATFORM, external_product_id=pid, external_variant_id=vid,
                                              external_sku=code, sku_id=sid))
                    db.flush()
            return sid
    key = title_key((item.get("purchased_item") or {}).get("title"))
    if key:
        return db.scalar(select(ExternalSkuMapping.sku_id).where(ExternalSkuMapping.platform == TITLE_PLATFORM,
                                                                 ExternalSkuMapping.external_product_id == key))
    return None


def _components(db: Session, sku_id: int, qty: int) -> list[tuple[int, int]]:
    sku = db.get(Sku, sku_id)
    if not sku.is_bundle:
        return [(sku_id, qty)]
    rows = db.execute(select(sku_bundle_components.c.component_sku_id, sku_bundle_components.c.qty)
                      .where(sku_bundle_components.c.bundle_sku_id == sku_id)).all()
    return [(cid, cq * qty) for cid, cq in rows]


# ---------- per-line state machine ----------

def _source_type(source: str) -> str:
    return {"webhook": "webhook", "api": "api", "csv": "csv"}.get(source, "system")


def _book_revenue(db: Session, so: SalesOrder, line: SalesOrderLine, source: str) -> None:
    _entry(db, f"whatnot:line:{line.id}:revenue", entry_date=biz_date(so.ordered_at),
           occurred_at=so.ordered_at, entry_type="revenue", amount_orig=line.line_amount,
           sales_order_id=so.id, order_line_id=line.id, livestream_id=so.livestream_id, sku_id=line.sku_id,
           source_type=_source_type(source), source_ref=f"whatnot:order:{so.external_order_id}")


def _reserve(db: Session, line: SalesOrderLine) -> None:
    us = inventory.location(db, "US_MAIN")
    for sku_id, qty in _components(db, line.sku_id, line.qty):
        exists = db.scalar(select(InventoryReservation).where(InventoryReservation.order_line_id == line.id,
                                                              InventoryReservation.sku_id == sku_id))
        if exists is None:
            db.add(InventoryReservation(order_line_id=line.id, sku_id=sku_id, location_id=us.id, qty=qty))
    db.flush()
    line.stock_state = "reserved"
    short = db.execute(text("""SELECT s.sku_code, v.us_available FROM inventory_reservations r
                                 JOIN v_inventory_position v ON v.sku_id = r.sku_id JOIN skus s ON s.id = r.sku_id
                                WHERE r.order_line_id = :l AND v.us_available < 0"""), {"l": line.id}).all()
    if short:
        _set_exception(line, "oversold", "美國可銷售庫存不足（已賣超過庫存）：" +
                       "、".join(f"{c} 可售 {a}" for c, a in short))


def _ship(db: Session, so: SalesOrder, line: SalesOrderLine, shipped_at: datetime, source: str) -> None:
    reservations = list(db.scalars(select(InventoryReservation).where(
        InventoryReservation.order_line_id == line.id, InventoryReservation.status == "reserved")))
    try:
        with db.begin_nested():
            for r in reservations:
                allocs = inventory.consume_fifo(
                    db, sku_id=r.sku_id, from_code="US_MAIN", to_code="CUSTOMER", movement_type="sale", qty=r.qty,
                    key_prefix=f"whatnot:line:{line.id}:sale:{r.sku_id}", ref_type="sales_order_line",
                    ref_id=line.id, note=f"Whatnot order {so.external_order_id}")
                for a in allocs:
                    total = q4(a.unit_cost_base * a.qty)
                    if not db.scalar(select(CogsAllocation.id).where(CogsAllocation.order_line_id == line.id,
                                                                     CogsAllocation.movement_id == a.movement_id)):
                        db.add(CogsAllocation(order_line_id=line.id, component_sku_id=r.sku_id, lot_id=a.lot_id,
                                              movement_id=a.movement_id, qty=a.qty, unit_cost_base=a.unit_cost_base,
                                              total_cost_base=total))
                    _entry(db, f"whatnot:line:{line.id}:cogs:{a.movement_id}",
                           entry_date=biz_date(shipped_at), occurred_at=shipped_at, entry_type="cogs",
                           amount_orig=-total, sales_order_id=so.id, order_line_id=line.id,
                           livestream_id=so.livestream_id, sku_id=r.sku_id, source_type=_source_type(source),
                           source_ref=f"whatnot:order:{so.external_order_id}")
                r.status = "consumed"
            db.flush()
    except AppError as e:
        if e.code != "insufficient_stock":
            raise
        d = e.details
        _set_exception(line, "insufficient_stock",
                       f"已出貨但美國倉帳上庫存不足：{d.get('sku_code')} 帳上 {d.get('available')}、需要 {d.get('requested')}；"
                       "請先登記入庫或盤點，系統下次同步會自動補扣")
        return
    line.stock_state = "shipped"
    line.shipped_at = shipped_at
    line.fulfillment_status = "shipped"
    if line.exception_code in ("insufficient_stock", "oversold"):
        _set_exception(line, None)


def _is_shipped(db: Session, so: SalesOrder, line: SalesOrderLine) -> datetime | None:
    if line.external_shipment_id:
        sh = db.scalar(select(SalesShipment).where(SalesShipment.platform == PLATFORM,
                                                   SalesShipment.external_id == line.external_shipment_id))
        if sh is not None and sh.status in SHIPPED_STATUSES:
            return sh.shipped_at or datetime.now(UTC)
    if so.status == "completed":
        return so.external_updated_at or datetime.now(UTC)
    return None


def apply_line(db: Session, so: SalesOrder, line: SalesOrderLine, source: str) -> None:
    now = datetime.now(UTC)
    if so.status in PAID:
        if so.currency != "USD":
            _set_exception(line, "non_usd", f"訂單幣別 {so.currency}：系統目前只處理 USD 訂單")
            return
        _book_revenue(db, so, line, source)
        if line.sku_id is None:
            _set_exception(line, "unmapped_sku", "找不到對應的 SKU：請在訂單頁指定，或在 Whatnot 商品設定 external ID = ERP 的 SKU")
            return
        if line.exception_code == "unmapped_sku":
            _set_exception(line, None)
        if line.stock_state == "none":
            _reserve(db, line)
        if line.stock_state == "reserved" and (shipped_at := _is_shipped(db, so, line)):
            _ship(db, so, line, shipped_at, source)
    elif so.status in DEAD:
        when = so.cancelled_at or now
        if line.stock_state == "reserved":
            for r in db.scalars(select(InventoryReservation).where(InventoryReservation.order_line_id == line.id,
                                                                   InventoryReservation.status == "reserved")):
                r.status, r.released_reason = "released", f"order {so.status}"
            line.stock_state = "released"
        elif line.stock_state == "shipped":
            _set_exception(line, "cancelled_after_shipping", "訂單在出貨後被取消：請確認是否退貨，退款以財務報表為準")
        _reverse(db, f"whatnot:line:{line.id}:revenue", when, _source_type(source))
        if line.exception_code in ("unmapped_sku", "oversold", "insufficient_stock"):
            _set_exception(line, None)


# ---------- orders ----------

def process_order(db: Session, order: dict, items: list[dict], *, source: str) -> SalesOrder:
    record_raw(db, "order", order["id"], {"order": order, "items": items}, source)
    so = db.scalar(select(SalesOrder).where(SalesOrder.platform == PLATFORM,
                                            SalesOrder.external_order_id == order["id"]).with_for_update())
    updated = parse_dt(order.get("updated_at"))
    if so is not None and so.external_updated_at and updated and updated < so.external_updated_at:
        return so  # an older version arriving late: never move backwards
    amounts = order.get("amounts") or {}
    total = amounts.get("total") or {}
    created = parse_dt(order.get("created_at")) or datetime.now(UTC)
    if so is None:
        so = SalesOrder(platform=PLATFORM, external_order_id=order["id"], ordered_at=created,
                        source="webhook" if source == "webhook" else "api" if source == "api" else "csv",
                        item_subtotal=0, total=0, status=order["status"])
        db.add(so)
    so.status = order["status"]
    so.currency = total.get("currency") or "USD"
    so.item_subtotal = money(amounts.get("subtotal")) or Decimal(0)
    so.shipping_charged = money(amounts.get("shipping")) or Decimal(0)
    so.sales_tax_collected = money(amounts.get("tax")) or Decimal(0)
    so.total = money(total) or Decimal(0)
    so.discount_total = abs(money(amounts.get("applied_discount")) or Decimal(0))
    so.transaction_type = order.get("transaction_type")
    so.cancelled_at = parse_dt(order.get("cancelled_at"))
    so.external_updated_at = updated
    so.last_synced_at = datetime.now(UTC)
    ls = livestream_for(db, order.get("show_id"), source)
    so.livestream_id = ls.id if ls else None
    if so.status in PAID and so.paid_at is None:
        so.paid_at = created
    db.flush()

    for item in items:
        line = db.scalar(select(SalesOrderLine).where(SalesOrderLine.order_id == so.id,
                                                      SalesOrderLine.external_line_id == item["id"]))
        unit = money(item.get("unit_price")) or Decimal(0)
        if line is None and source != "csv":
            # The same order may have been imported from a CSV export first: take over that line
            # (same quantity and price) instead of creating a second one -> no double revenue.
            line = db.scalar(select(SalesOrderLine).where(
                SalesOrderLine.order_id == so.id, SalesOrderLine.external_line_id.like("csv-%"),
                SalesOrderLine.qty == item["quantity"], SalesOrderLine.unit_price == unit).limit(1))
            if line is not None:
                line.external_line_id = item["id"]
        if line is None:
            line = SalesOrderLine(order_id=so.id, external_line_id=item["id"], qty=item["quantity"], unit_price=unit,
                                  line_amount=q4(unit * item["quantity"]))
            db.add(line)
        purchased = item.get("purchased_item") or {}
        line.title_snapshot = (purchased.get("title") or "")[:500] or None
        line.purchased_sku = purchased.get("sku")
        line.external_product_id = item.get("product_id")
        line.external_variant_id = item.get("variant_id")
        line.external_product_ref = item.get("variant_external_id") or item.get("product_external_id")
        line.external_shipment_id = item.get("shipment_id") or line.external_shipment_id
        if line.stock_state == "none":  # quantity / price can only change before anything was booked
            line.qty, line.unit_price, line.line_amount = item["quantity"], unit, q4(unit * item["quantity"])
        if line.sku_id is None:
            sid = resolve_sku(db, item)
            if sid:
                line.sku_id, line.mapping_status = sid, "mapped"
        db.flush()
        apply_line(db, so, line, source)

    if so.discount_total and so.status in PAID and so.currency == "USD":
        _entry(db, f"whatnot:order:{so.external_order_id}:discount", entry_date=biz_date(so.ordered_at),
               occurred_at=so.ordered_at, entry_type="sales_discount", amount_orig=-so.discount_total,
               sales_order_id=so.id, livestream_id=so.livestream_id, source_type=_source_type(source),
               source_ref=f"whatnot:order:{so.external_order_id}")
    elif so.status in DEAD:
        _reverse(db, f"whatnot:order:{so.external_order_id}:discount", so.cancelled_at or datetime.now(UTC),
                 _source_type(source))
    db.flush()
    return so


def reprocess_order(db: Session, so: SalesOrder, source: str = "system") -> None:
    """Re-run the state machine with stored data (after a manual SKU mapping, a stock receipt...)."""
    for line in db.scalars(select(SalesOrderLine).where(SalesOrderLine.order_id == so.id)):
        apply_line(db, so, line, source)
    db.flush()


# ---------- shipments ----------

def process_shipment(db: Session, shipment: dict, items: list[dict], *, source: str) -> SalesShipment:
    record_raw(db, "shipment", shipment["id"], {"shipment": shipment, "items": items}, source)
    sh = db.scalar(select(SalesShipment).where(SalesShipment.platform == PLATFORM,
                                               SalesShipment.external_id == shipment["id"]).with_for_update())
    updated = parse_dt(shipment.get("updated_at"))
    if sh is not None and sh.external_updated_at and updated and updated < sh.external_updated_at:
        return sh
    if sh is None:
        sh = SalesShipment(platform=PLATFORM, external_id=shipment["id"], status=shipment["status"])
        db.add(sh)
    sh.status = shipment["status"]
    tracking = shipment.get("tracking") or {}
    sh.carrier = tracking.get("carrier")
    sh.tracking_code = tracking.get("tracking_code") or tracking.get("code")
    cost = shipment.get("seller_paid_shipping_cost")
    sh.seller_paid_shipping_cost, sh.currency = money(cost), (cost or {}).get("currency")
    sh.external_updated_at = updated
    if sh.status in SHIPPED_STATUSES and sh.shipped_at is None:
        sh.shipped_at = updated or datetime.now(UTC)
    db.flush()
    item_ids = [i["order_item_id"] for i in items if i.get("order_item_id")]
    if item_ids:
        lines = list(db.scalars(select(SalesOrderLine).where(SalesOrderLine.external_line_id.in_(item_ids))))
        for line in lines:
            line.external_shipment_id = sh.external_id
        db.flush()
        for line in lines:
            so = db.get(SalesOrder, line.order_id)
            apply_line(db, so, line, source)
    return sh
