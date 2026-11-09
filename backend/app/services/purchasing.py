"""Purchase batches (read side) and the physical flow of purchased goods.

Owner's flow (2026-11-09): goods are bought in Japan and shipped STRAIGHT to the US.

  ship from Japan   SUPPLIER   -> IN_TRANSIT   creates a FIFO lot at the line's landed unit cost;
                                               not sellable yet; ETA = ship date + lead time (9 days)
  receive in the US IN_TRANSIT -> US_MAIN      only now does the stock become sellable

Partial shipments / receipts are allowed. Shipped can never exceed purchased, and received
can never exceed what is in transit for that line. Both actions are idempotent.
"""
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.inventory import InventoryLot, InventoryMovement
from app.models.purchasing import PurchaseLine
from app.models.replenishment import ReplenishmentSettings
from app.services import audit
from app.services.audit import RequestContext
from app.services.inventory import location, post_movement

LINE_STATUS_SQL = """
    SELECT COALESCE(sum(l.original_qty), 0)::int AS shipped,
           COALESCE((SELECT sum(b.qty) FROM inventory_balances b JOIN inventory_lots x ON x.id = b.lot_id
                      WHERE x.purchase_line_id = :line AND b.location_id = :transit), 0)::int AS in_transit,
           COALESCE((SELECT sum(m.qty) FROM inventory_movements m JOIN inventory_lots x ON x.id = m.lot_id
                      WHERE x.purchase_line_id = :line AND m.movement_type = 'transfer_receive'), 0)::int AS received_us
      FROM inventory_lots l WHERE l.purchase_line_id = :line
"""


def _now() -> datetime:
    return datetime.now(UTC)


def lead_time_days(db: Session) -> int:
    s = db.get(ReplenishmentSettings, 1)
    return s.lead_time_days if s else 9


def line_status(db: Session, line_id: int) -> dict:
    return dict(db.execute(text(LINE_STATUS_SQL), {"line": line_id, "transit": location(db, "IN_TRANSIT").id})
                .mappings().one())


def _when(d: date | None) -> datetime:
    today = _now().date()
    if d and d > today:
        raise AppError("future_date", "Date cannot be in the future", 422)
    return datetime.combine(d, time(12, 0), tzinfo=UTC) if d else _now()


def _lock_line(db: Session, line_id: int) -> PurchaseLine:
    # Row lock: two people acting on the same line at once can't exceed the purchased quantity.
    line = db.scalar(select(PurchaseLine).where(PurchaseLine.id == line_id).with_for_update())
    if line is None:
        raise AppError("not_found", "Purchase line not found", 404)
    return line


def ship_line(db: Session, *, actor_id: int, line_id: int, qty: int, shipped_on: date | None,
              idempotency_key: str, ctx: RequestContext) -> InventoryMovement:
    key = f"ship:{idempotency_key}"
    if existing := db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key)):
        return existing
    line = _lock_line(db, line_id)
    if qty <= 0:
        raise AppError("invalid_qty", "Quantity must be at least 1", 422)
    shipped = line_status(db, line.id)["shipped"]
    if shipped + qty > line.qty:
        raise AppError("over_shipment", "Cannot ship more than was purchased", 409,
                       {"purchased": line.qty, "already_shipped": shipped, "requested": qty})
    when = _when(shipped_on)
    lot = InventoryLot(sku_id=line.sku_id, lot_no=f"PL{line.id}-{shipped + 1}-{shipped + qty}", source="purchase",
                       purchase_line_id=line.id, received_at=when, original_qty=qty,
                       original_unit_cost_base=line.landed_unit_cost_base)
    db.add(lot)
    db.flush()
    eta = (when + timedelta(days=lead_time_days(db))).date()
    move = post_movement(db, sku_id=line.sku_id, lot_id=lot.id, movement_type="purchase_receipt",
                         from_code="SUPPLIER", to_code="IN_TRANSIT", qty=qty, key=key, ref_type="purchase_line",
                         ref_id=line.id, actor_id=actor_id, occurred_at=when,
                         note=f"shipped from Japan {shipped + qty}/{line.qty}; ETA {eta.isoformat()}")
    audit.record(db, action="purchase_line.shipped", entity_type="purchase_line", entity_id=line.id,
                 actor_id=actor_id, after={"qty": qty, "shipped_total": shipped + qty, "purchased": line.qty,
                                           "lot_id": lot.id, "eta": eta.isoformat(),
                                           "unit_cost_usd": str(line.landed_unit_cost_base)}, ctx=ctx)
    return move


def receive_line_in_us(db: Session, *, actor_id: int, line_id: int, qty: int, received_on: date | None,
                       idempotency_key: str, ctx: RequestContext) -> list[InventoryMovement]:
    prefix = f"usrecv:{idempotency_key}"
    existing = list(db.scalars(select(InventoryMovement)
                               .where(InventoryMovement.idempotency_key.like(f"{prefix}:lot:%"))))
    if existing:
        return existing
    line = _lock_line(db, line_id)
    if qty <= 0:
        raise AppError("invalid_qty", "Quantity must be at least 1", 422)
    transit = location(db, "IN_TRANSIT")
    rows = db.execute(text("""
        SELECT b.lot_id, b.qty FROM inventory_balances b JOIN inventory_lots l ON l.id = b.lot_id
         WHERE l.purchase_line_id = :line AND b.location_id = :transit AND b.qty > 0
         ORDER BY l.received_at, l.id
           FOR UPDATE OF b"""), {"line": line.id, "transit": transit.id}).all()
    available = sum(r.qty for r in rows)
    if qty > available:
        raise AppError("over_receipt", "Cannot receive more than is in transit for this line", 409,
                       {"in_transit": available, "requested": qty})
    when = _when(received_on)
    moves, remaining = [], qty
    for r in rows:  # earliest shipment first
        if remaining == 0:
            break
        take = min(r.qty, remaining)
        m = post_movement(db, sku_id=line.sku_id, lot_id=r.lot_id, movement_type="transfer_receive",
                          from_code="IN_TRANSIT", to_code="US_MAIN", qty=take, key=f"{prefix}:lot:{r.lot_id}",
                          ref_type="purchase_line", ref_id=line.id, actor_id=actor_id, note="received in the US",
                          occurred_at=when)
        moves.append(m)
        remaining -= take
    db.flush()
    st = line_status(db, line.id)
    audit.record(db, action="purchase_line.received_us", entity_type="purchase_line", entity_id=line.id,
                 actor_id=actor_id, after={"qty": qty, "received_total": st["received_us"], "purchased": line.qty},
                 ctx=ctx)
    return moves


def list_batches(db: Session, *, search: str | None, only_open: bool, with_cost: bool, limit: int = 200) -> list[dict]:
    where, params = ["TRUE"], {"limit": limit, "transit": location(db, "IN_TRANSIT").id}
    if search:
        where.append("(b.batch_no ILIKE :q OR EXISTS (SELECT 1 FROM purchase_lines pl JOIN skus s ON s.id = pl.sku_id "
                     "WHERE pl.batch_id = b.id AND (s.sku_code ILIKE :q OR s.name ILIKE :q)))")
        params["q"] = f"%{search}%"
    rows = db.execute(text(f"""
        WITH lots AS (
            SELECT l.purchase_line_id AS line_id, sum(l.original_qty) AS shipped,
                   sum(COALESCE((SELECT sum(b.qty) FROM inventory_balances b WHERE b.lot_id = l.id
                                  AND b.location_id = :transit), 0)) AS in_transit,
                   sum(COALESCE((SELECT sum(m.qty) FROM inventory_movements m WHERE m.lot_id = l.id
                                  AND m.movement_type = 'transfer_receive'), 0)) AS received_us
              FROM inventory_lots l WHERE l.purchase_line_id IS NOT NULL GROUP BY l.purchase_line_id),
        lines AS (
            SELECT pl.batch_id, count(*) AS line_count, sum(pl.qty) AS qty, sum(pl.landed_total_base) AS landed_total,
                   sum(COALESCE(x.shipped, 0)) AS shipped, sum(COALESCE(x.in_transit, 0)) AS in_transit,
                   sum(COALESCE(x.received_us, 0)) AS received_us
              FROM purchase_lines pl LEFT JOIN lots x ON x.line_id = pl.id
             GROUP BY pl.batch_id)
        SELECT b.id, b.batch_no, b.purchase_date, b.status, s.name AS supplier, b.import_job_id,
               l.line_count, l.qty::int, l.shipped::int, l.in_transit::int, l.received_us::int, l.landed_total
          FROM purchase_batches b
          JOIN lines l ON l.batch_id = b.id
          LEFT JOIN suppliers s ON s.id = b.supplier_id
         WHERE {' AND '.join(where)}
         ORDER BY b.purchase_date DESC, b.id DESC LIMIT :limit
    """), params).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        if only_open and d["received_us"] >= d["qty"]:
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
               pl.duty_other_base, pl.landed_total_base, pl.landed_unit_cost_base, pl.note
          FROM purchase_lines pl JOIN skus s ON s.id = pl.sku_id
         WHERE pl.batch_id = :b ORDER BY pl.line_no"""), {"b": batch_id}).mappings().all()
    lead = lead_time_days(db)
    cost_keys = ("unit_price_orig", "currency_orig", "fx_rate_to_base", "fx_rate_quote", "purchase_cost_base",
                 "domestic_shipping_jpy", "intl_freight_jpy", "domestic_shipping_base",
                 "intl_freight_base", "duty_other_base", "landed_total_base", "landed_unit_cost_base")
    out_lines = []
    for l in lines:
        d = dict(l)
        d.update(line_status(db, d["id"]))
        shipments = db.execute(text("""
            SELECT l.received_at AS shipped_at, l.original_qty AS qty,
                   COALESCE((SELECT sum(b.qty) FROM inventory_balances b JOIN locations loc ON loc.id = b.location_id
                              WHERE b.lot_id = l.id AND loc.code = 'IN_TRANSIT'), 0)::int AS in_transit
              FROM inventory_lots l WHERE l.purchase_line_id = :line ORDER BY l.received_at, l.id"""),
            {"line": d["id"]}).mappings().all()
        d["shipments"] = [{**dict(s), "eta": (s["shipped_at"] + timedelta(days=lead)).date()} for s in shipments]
        if not with_cost:
            for k in cost_keys:
                d.pop(k)
        out_lines.append(d)
    return {**dict(b), "lines": out_lines, "can_view_cost": with_cost, "lead_time_days": lead}
