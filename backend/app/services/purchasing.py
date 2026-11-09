"""Purchase batches (read side) and receiving purchased goods into the Japan warehouse.

Receiving creates a FIFO lot whose unit cost is the line's landed unit cost (USD) and moves
the units SUPPLIER -> JP_MAIN. Partial receipts are allowed; the total received can never
exceed the purchased quantity.
"""
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.inventory import InventoryLot, InventoryMovement
from app.models.purchasing import PurchaseLine
from app.services import audit
from app.services.audit import RequestContext
from app.services.inventory import post_movement


def received_qty(db: Session, line_id: int) -> int:
    return db.scalar(select(func.coalesce(func.sum(InventoryLot.original_qty), 0))
                     .where(InventoryLot.purchase_line_id == line_id))


def receive_line(db: Session, *, actor_id: int, line_id: int, qty: int, received_on: date | None,
                 idempotency_key: str, ctx: RequestContext) -> InventoryMovement:
    key = f"receive:{idempotency_key}"
    existing = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key))
    if existing:
        return existing
    # Lock the line so two people receiving at the same time can't exceed the purchased qty.
    line = db.scalar(select(PurchaseLine).where(PurchaseLine.id == line_id).with_for_update())
    if line is None:
        raise AppError("not_found", "Purchase line not found", 404)
    if qty <= 0:
        raise AppError("invalid_qty", "Quantity must be at least 1", 422)
    already = received_qty(db, line.id)
    if already + qty > line.qty:
        raise AppError("over_receipt", "Cannot receive more than was purchased", 409,
                       {"purchased": line.qty, "already_received": already, "requested": qty})
    today = datetime.now(UTC).date()
    if received_on and received_on > today:
        raise AppError("future_date", "Receiving date cannot be in the future", 422)
    # FIFO order follows the real arrival date (earlier arrivals are sold first).
    when = datetime.combine(received_on, time(12, 0), tzinfo=UTC) if received_on else datetime.now(UTC)
    lot = InventoryLot(sku_id=line.sku_id, lot_no=f"PL{line.id}-{already + 1}-{already + qty}", source="purchase",
                       purchase_line_id=line.id, received_at=when, original_qty=qty,
                       original_unit_cost_base=line.landed_unit_cost_base)
    db.add(lot)
    db.flush()
    move = post_movement(db, sku_id=line.sku_id, lot_id=lot.id, movement_type="purchase_receipt",
                         from_code="SUPPLIER", to_code="JP_MAIN", qty=qty, key=key, ref_type="purchase_line",
                         ref_id=line.id, actor_id=actor_id, note=f"received {already + qty}/{line.qty}")
    audit.record(db, action="purchase_line.received", entity_type="purchase_line", entity_id=line.id,
                 actor_id=actor_id, after={"qty": qty, "received_total": already + qty, "purchased": line.qty,
                                           "lot_id": lot.id, "unit_cost_usd": str(line.landed_unit_cost_base)},
                 ctx=ctx)
    return move


def list_batches(db: Session, *, search: str | None, only_open: bool, with_cost: bool, limit: int = 200) -> list[dict]:
    where, params = ["TRUE"], {"limit": limit}
    if search:
        where.append("(b.batch_no ILIKE :q OR EXISTS (SELECT 1 FROM purchase_lines pl JOIN skus s ON s.id = pl.sku_id "
                     "WHERE pl.batch_id = b.id AND (s.sku_code ILIKE :q OR s.name ILIKE :q)))")
        params["q"] = f"%{search}%"
    rows = db.execute(text(f"""
        WITH lines AS (
            SELECT pl.batch_id, count(*) AS line_count, sum(pl.qty) AS qty, sum(pl.landed_total_base) AS landed_total,
                   sum(COALESCE(r.received, 0)) AS received
              FROM purchase_lines pl
              LEFT JOIN (SELECT purchase_line_id, sum(original_qty) AS received FROM inventory_lots
                          WHERE purchase_line_id IS NOT NULL GROUP BY purchase_line_id) r ON r.purchase_line_id = pl.id
             GROUP BY pl.batch_id)
        SELECT b.id, b.batch_no, b.purchase_date, b.status, s.name AS supplier, b.import_job_id,
               l.line_count, l.qty, l.received, l.landed_total
          FROM purchase_batches b
          JOIN lines l ON l.batch_id = b.id
          LEFT JOIN suppliers s ON s.id = b.supplier_id
         WHERE {' AND '.join(where)}
         ORDER BY b.purchase_date DESC, b.id DESC LIMIT :limit
    """), params).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        if only_open and d["received"] >= d["qty"]:
            continue
        if not with_cost:
            d.pop("landed_total")
        out.append(d)
    return out


def batch_detail(db: Session, batch_id: int, *, with_cost: bool) -> dict:
    b = db.execute(text("""SELECT b.id, b.batch_no, b.purchase_date, b.status, b.confirmed_at, b.import_job_id,
                                  s.name AS supplier
                             FROM purchase_batches b LEFT JOIN suppliers s ON s.id = b.supplier_id
                            WHERE b.id = :b"""), {"b": batch_id}).mappings().first()
    if b is None:
        raise AppError("not_found", "Purchase batch not found", 404)
    lines = db.execute(text("""
        SELECT pl.id, pl.line_no, pl.sku_id, s.sku_code, s.name, pl.qty, pl.unit_price_orig, pl.currency_orig,
               pl.fx_rate_to_base, pl.fx_rate_quote, pl.purchase_cost_base, pl.domestic_shipping_jpy,
               pl.intl_freight_jpy, pl.domestic_shipping_base, pl.intl_freight_base,
               pl.duty_other_base, pl.landed_total_base, pl.landed_unit_cost_base, pl.note,
               COALESCE((SELECT sum(original_qty) FROM inventory_lots WHERE purchase_line_id = pl.id), 0)::int AS received
          FROM purchase_lines pl JOIN skus s ON s.id = pl.sku_id
         WHERE pl.batch_id = :b ORDER BY pl.line_no"""), {"b": batch_id}).mappings().all()
    cost_keys = ("unit_price_orig", "currency_orig", "fx_rate_to_base", "fx_rate_quote", "purchase_cost_base",
                 "domestic_shipping_jpy", "intl_freight_jpy", "domestic_shipping_base",
                 "intl_freight_base", "duty_other_base", "landed_total_base", "landed_unit_cost_base")
    out_lines = []
    for l in lines:
        d = dict(l)
        if not with_cost:
            for k in cost_keys:
                d.pop(k)
        out_lines.append(d)
    return {**dict(b), "lines": out_lines, "can_view_cost": with_cost}


def money(v) -> str:
    return str(Decimal(v).quantize(Decimal("0.01")))
