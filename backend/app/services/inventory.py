"""Inventory operations on top of the database-enforced ledger.

Every operation:
  * runs in the caller's transaction (all lines succeed or none do)
  * takes an idempotency key (from the client's Idempotency-Key header); repeating a request
    with the same key returns the original result instead of moving stock twice
  * is audited

FIFO: stock leaving a location is taken from the oldest lot (by received_at) first.
Location permissions: JP_MAIN needs inventory.adjust_jp, US_MAIN needs inventory.adjust_us.
In-transit stock is only changed by shipments (Phase 9).
"""
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.catalog import Sku
from app.models.inventory import (
    InventoryBalance, InventoryLot, InventoryMovement, StockCount, StockCountLine,
)
from app.models.reference import Location
from app.services import audit
from app.services.audit import RequestContext

STOCK_LOCATIONS = {"JP_MAIN": "inventory.adjust_jp", "US_MAIN": "inventory.adjust_us"}


def _now() -> datetime:
    return datetime.now(UTC)


def location(db: Session, code: str) -> Location:
    loc = db.scalar(select(Location).where(Location.code == code))
    if loc is None:
        raise AppError("unknown_location", f"Unknown location {code}", 422)
    return loc


def require_location_permission(code: str, permissions: set[str]) -> None:
    from app.core.config import get_settings
    if code == "JP_MAIN" and not get_settings().japan_warehouse_enabled:
        raise AppError("japan_warehouse_disabled", "The Japan warehouse is not used (goods ship directly to the US)", 422)
    perm = STOCK_LOCATIONS.get(code)
    if perm is None:
        raise AppError("location_not_adjustable", "Stock can only be adjusted in JP_MAIN or US_MAIN", 422)
    if perm not in permissions:
        raise AppError("forbidden", "You do not have permission for this warehouse", 403, {"missing": [perm]})


def _stock_sku(db: Session, sku_id: int) -> Sku:
    sku = db.get(Sku, sku_id)
    if sku is None:
        raise AppError("not_found", "SKU not found", 404, {"sku_id": sku_id})
    if sku.is_bundle:
        raise AppError("bundle_no_stock", "Bundles hold no stock; adjust the component SKUs", 422,
                       {"sku_code": sku.sku_code})
    return sku


@dataclass
class Allocation:
    lot_id: int
    qty: int
    unit_cost_base: Decimal
    movement_id: int


def post_movement(db: Session, *, sku_id: int, lot_id: int, movement_type: str, from_code: str, to_code: str,
                  qty: int, key: str, ref_type: str | None = None, ref_id: int | None = None,
                  actor_id: int | None = None, note: str | None = None,
                  occurred_at: datetime | None = None) -> InventoryMovement:
    m = InventoryMovement(
        sku_id=sku_id, lot_id=lot_id, movement_type=movement_type,
        from_location_id=location(db, from_code).id, to_location_id=location(db, to_code).id,
        qty=qty, idempotency_key=key, ref_type=ref_type, ref_id=ref_id, created_by=actor_id, note=note,
        **({"occurred_at": occurred_at} if occurred_at else {}),
    )
    db.add(m)
    db.flush()  # triggers validate direction and update balances here
    return m


def _lot_unit_cost(db: Session, lot_id: int) -> Decimal:
    return db.execute(text("SELECT unit_cost_base FROM v_lot_cost WHERE lot_id = :l"), {"l": lot_id}).scalar_one()


def consume_fifo(db: Session, *, sku_id: int, from_code: str, to_code: str, movement_type: str, qty: int,
                 key_prefix: str, ref_type: str | None = None, ref_id: int | None = None,
                 actor_id: int | None = None, note: str | None = None) -> list[Allocation]:
    """Take `qty` units out of `from_code`, oldest lot first. Idempotent per key_prefix."""
    if qty <= 0:
        raise AppError("invalid_qty", "Quantity must be at least 1", 422)
    existing = list(db.scalars(
        select(InventoryMovement).where(InventoryMovement.idempotency_key.like(f"{key_prefix}:lot:%"))
        .order_by(InventoryMovement.id)
    ))
    if existing:
        return [Allocation(m.lot_id, m.qty, _lot_unit_cost(db, m.lot_id), m.id) for m in existing]

    loc = location(db, from_code)
    rows = db.execute(
        select(InventoryBalance.lot_id, InventoryBalance.qty)
        .join(InventoryLot, InventoryLot.id == InventoryBalance.lot_id)
        .where(InventoryBalance.sku_id == sku_id, InventoryBalance.location_id == loc.id, InventoryBalance.qty > 0)
        .order_by(InventoryLot.received_at, InventoryLot.id)
        .with_for_update(of=InventoryBalance)
    ).all()
    available = sum(r.qty for r in rows)
    if available < qty:
        sku = db.get(Sku, sku_id)
        raise AppError("insufficient_stock", "Not enough stock at this location", 409,
                       {"sku_code": sku.sku_code if sku else sku_id, "location": from_code,
                        "available": available, "requested": qty})
    out, remaining = [], qty
    for r in rows:
        if remaining == 0:
            break
        take = min(r.qty, remaining)
        m = post_movement(db, sku_id=sku_id, lot_id=r.lot_id, movement_type=movement_type, from_code=from_code,
                          to_code=to_code, qty=take, key=f"{key_prefix}:lot:{r.lot_id}", ref_type=ref_type,
                          ref_id=ref_id, actor_id=actor_id, note=note)
        out.append(Allocation(r.lot_id, take, _lot_unit_cost(db, r.lot_id), m.id))
        remaining -= take
    return out


def _replayed(db: Session, prefix: str) -> list[InventoryMovement]:
    return list(db.scalars(select(InventoryMovement).where(InventoryMovement.idempotency_key.like(f"{prefix}:%"))
                           .order_by(InventoryMovement.id)))


def _new_lot(db: Session, *, sku_id: int, qty: int, unit_cost: Decimal, source: str) -> InventoryLot:
    if unit_cost is None or unit_cost < 0:
        raise AppError("unit_cost_required", "Unit cost (USD) is required and cannot be negative", 422)
    lot = InventoryLot(sku_id=sku_id, lot_no=f"{source[:3].upper()}-{_now():%Y%m%d}-{_uid()}", source=source,
                       received_at=_now(), original_qty=qty, original_unit_cost_base=unit_cost)
    db.add(lot)
    db.flush()
    return lot


def _uid() -> str:
    import uuid
    return uuid.uuid4().hex[:8].upper()


# ---------- opening stock ----------

def opening_stock(db: Session, *, actor_id: int, location_code: str, lines: list[dict], idempotency_key: str,
                  note: str | None, ctx: RequestContext) -> list[InventoryMovement]:
    """Initial stock take when starting to use the ERP. Each line creates a lot with its USD unit cost."""
    prefix = f"opening:{idempotency_key}"
    if replay := _replayed(db, prefix):
        return replay
    if not lines:
        raise AppError("lines_required", "At least one line is required", 422)
    moves = []
    for i, line in enumerate(lines):
        _stock_sku(db, line["sku_id"])
        if line["qty"] <= 0:
            raise AppError("invalid_qty", "Quantity must be at least 1", 422, {"line": i + 1})
        lot = _new_lot(db, sku_id=line["sku_id"], qty=line["qty"], unit_cost=line.get("unit_cost_usd"),
                       source="opening_balance")
        moves.append(post_movement(db, sku_id=line["sku_id"], lot_id=lot.id, movement_type="opening_balance",
                                   from_code="ADJUSTMENT", to_code=location_code, qty=line["qty"],
                                   key=f"{prefix}:{i}", ref_type="opening_stock", actor_id=actor_id, note=note))
    audit.record(db, action="inventory.opening_stock", entity_type="location", entity_id=location_code,
                 actor_id=actor_id, after={"lines": [{**l, "unit_cost_usd": str(l.get("unit_cost_usd"))} for l in lines],
                                           "note": note, "idempotency_key": idempotency_key}, ctx=ctx)
    return moves


# ---------- adjustments ----------

ADJUSTMENT_KINDS = ("increase", "decrease", "damage")


def _increase(db: Session, *, sku_id: int, location_code: str, qty: int, unit_cost, key: str, ref_type: str,
              ref_id: int | None, actor_id: int, note: str | None) -> InventoryMovement:
    """Found stock goes into the SKU's newest lot (same cost); if the SKU has no lot yet a unit cost is required."""
    lot_id = db.scalar(select(InventoryLot.id).where(InventoryLot.sku_id == sku_id)
                       .order_by(InventoryLot.received_at.desc(), InventoryLot.id.desc()).limit(1))
    if lot_id is None:
        if unit_cost is None:
            raise AppError("unit_cost_required", "This SKU has no cost history yet; enter a unit cost (USD)", 422,
                           {"sku_id": sku_id})
        lot_id = _new_lot(db, sku_id=sku_id, qty=qty, unit_cost=unit_cost, source="adjustment").id
    return post_movement(db, sku_id=sku_id, lot_id=lot_id, movement_type="adjustment_in", from_code="ADJUSTMENT",
                         to_code=location_code, qty=qty, key=key, ref_type=ref_type, ref_id=ref_id,
                         actor_id=actor_id, note=note)


def adjust(db: Session, *, actor_id: int, location_code: str, kind: str, reason: str, lines: list[dict],
           idempotency_key: str, ctx: RequestContext) -> list[InventoryMovement]:
    if kind not in ADJUSTMENT_KINDS:
        raise AppError("invalid_kind", f"kind must be one of {ADJUSTMENT_KINDS}", 422)
    if not reason or not reason.strip():
        raise AppError("reason_required", "A reason is required for stock adjustments", 422)
    prefix = f"adjust:{idempotency_key}"
    if replay := _replayed(db, prefix):
        return replay
    if not lines:
        raise AppError("lines_required", "At least one line is required", 422)
    for i, line in enumerate(lines):
        _stock_sku(db, line["sku_id"])
        if line["qty"] <= 0:
            raise AppError("invalid_qty", "Quantity must be at least 1", 422, {"line": i + 1})
        if kind == "increase":
            _increase(db, sku_id=line["sku_id"], location_code=location_code, qty=line["qty"],
                      unit_cost=line.get("unit_cost_usd"), key=f"{prefix}:{i}", ref_type="adjustment", ref_id=None,
                      actor_id=actor_id, note=reason)
        else:
            consume_fifo(db, sku_id=line["sku_id"], from_code=location_code,
                         to_code="LOSS" if kind == "damage" else "ADJUSTMENT",
                         movement_type="damage" if kind == "damage" else "adjustment_out", qty=line["qty"],
                         key_prefix=f"{prefix}:{i}", ref_type="adjustment", actor_id=actor_id, note=reason)
    audit.record(db, action=f"inventory.adjust_{kind}", entity_type="location", entity_id=location_code,
                 actor_id=actor_id, after={"reason": reason, "lines": [{**l, "unit_cost_usd": str(l.get("unit_cost_usd"))}
                                                                      for l in lines],
                                           "idempotency_key": idempotency_key}, ctx=ctx)
    return _replayed(db, prefix)


# ---------- stock counts ----------

def _on_hand(db: Session, sku_id: int, location_id: int) -> int:
    return db.scalar(select(func.coalesce(func.sum(InventoryBalance.qty), 0))
                     .where(InventoryBalance.sku_id == sku_id, InventoryBalance.location_id == location_id))


def get_count(db: Session, count_id: int, *, for_update: bool = False) -> StockCount:
    sc = db.get(StockCount, count_id, with_for_update=for_update, populate_existing=for_update)
    if sc is None:
        raise AppError("not_found", "Stock count not found", 404)
    return sc


def create_count(db: Session, *, actor_id: int, location_code: str, sku_ids: list[int] | None, note: str | None,
                 ctx: RequestContext) -> StockCount:
    loc = location(db, location_code)
    q = select(Sku.id).where(~Sku.is_bundle, Sku.status == "active").order_by(Sku.sku_code)
    if sku_ids:
        q = select(Sku.id).where(Sku.id.in_(sku_ids), ~Sku.is_bundle).order_by(Sku.sku_code)
    ids = list(db.scalars(q))
    if not ids:
        raise AppError("nothing_to_count", "No SKUs to count", 422)
    sc = StockCount(location_id=loc.id, created_by=actor_id, note=note)
    db.add(sc)
    db.flush()
    for sid in ids:
        db.add(StockCountLine(stock_count_id=sc.id, sku_id=sid, expected_qty=_on_hand(db, sid, loc.id)))
    db.flush()
    audit.record(db, action="stock_count.created", entity_type="stock_count", entity_id=sc.id, actor_id=actor_id,
                 after={"location": location_code, "sku_count": len(ids)}, ctx=ctx)
    return sc


def record_counts(db: Session, *, actor_id: int, count_id: int, counts: list[dict], ctx: RequestContext) -> StockCount:
    sc = get_count(db, count_id, for_update=True)
    if sc.status != "draft":
        raise AppError("count_not_editable", f"Stock count is {sc.status}", 409)
    lines = {l.sku_id: l for l in db.scalars(select(StockCountLine).where(StockCountLine.stock_count_id == sc.id))}
    for c in counts:
        line = lines.get(c["sku_id"])
        if line is None:
            raise AppError("not_in_count", "SKU is not part of this stock count", 422, {"sku_id": c["sku_id"]})
        if c["counted_qty"] is not None and c["counted_qty"] < 0:
            raise AppError("invalid_qty", "Counted quantity cannot be negative", 422)
        line.counted_qty = c["counted_qty"]
    sc.counted_at = _now()
    db.flush()
    return sc


def submit_count(db: Session, *, actor_id: int, count_id: int, ctx: RequestContext) -> StockCount:
    sc = get_count(db, count_id, for_update=True)
    if sc.status != "draft":
        raise AppError("count_not_editable", f"Stock count is {sc.status}", 409)
    missing = db.scalar(select(func.count()).select_from(StockCountLine)
                        .where(StockCountLine.stock_count_id == sc.id, StockCountLine.counted_qty.is_(None)))
    if missing:
        raise AppError("count_incomplete", "Every SKU needs a counted quantity before submitting", 422,
                       {"missing": missing})
    sc.status, sc.submitted_by, sc.submitted_at = "submitted", actor_id, _now()
    audit.record(db, action="stock_count.submitted", entity_type="stock_count", entity_id=sc.id, actor_id=actor_id,
                 ctx=ctx)
    return sc


def approve_count(db: Session, *, actor_id: int, count_id: int, ctx: RequestContext) -> StockCount:
    """Posts the differences. The system quantity is re-read at approval so stock that moved
    between counting and approval is not double-corrected (count when the warehouse is idle)."""
    sc = get_count(db, count_id, for_update=True)
    if sc.status != "submitted":
        raise AppError("count_not_submitted", "Only a submitted stock count can be approved", 409)
    loc = db.get(Location, sc.location_id)
    diffs = []
    for line in db.scalars(select(StockCountLine).where(StockCountLine.stock_count_id == sc.id)
                           .order_by(StockCountLine.sku_id)):
        current = _on_hand(db, line.sku_id, loc.id)
        line.expected_qty = current
        line.difference_qty = line.counted_qty - current
        key = f"count:{sc.id}:sku:{line.sku_id}"
        if line.difference_qty > 0:
            _increase(db, sku_id=line.sku_id, location_code=loc.code, qty=line.difference_qty, unit_cost=None,
                      key=key, ref_type="stock_count", ref_id=sc.id, actor_id=actor_id, note="stock count gain")
        elif line.difference_qty < 0:
            consume_fifo(db, sku_id=line.sku_id, from_code=loc.code, to_code="ADJUSTMENT",
                         movement_type="adjustment_out", qty=-line.difference_qty, key_prefix=key,
                         ref_type="stock_count", ref_id=sc.id, actor_id=actor_id, note="stock count loss")
        if line.difference_qty:
            diffs.append({"sku_id": line.sku_id, "system": current, "counted": line.counted_qty,
                          "difference": line.difference_qty})
    sc.status, sc.approved_by, sc.approved_at = "approved", actor_id, _now()
    audit.record(db, action="stock_count.approved", entity_type="stock_count", entity_id=sc.id, actor_id=actor_id,
                 after={"location": loc.code, "differences": diffs}, ctx=ctx)
    return sc


def cancel_count(db: Session, *, actor_id: int, count_id: int, ctx: RequestContext) -> StockCount:
    sc = get_count(db, count_id, for_update=True)
    if sc.status not in ("draft", "submitted"):
        raise AppError("count_not_editable", f"Stock count is {sc.status}", 409)
    sc.status = "cancelled"
    audit.record(db, action="stock_count.cancelled", entity_type="stock_count", entity_id=sc.id, actor_id=actor_id,
                 ctx=ctx)
    return sc
