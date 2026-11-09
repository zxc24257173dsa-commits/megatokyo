"""Sealed boxes and opening them into their contents.

Owner rule (2026-11-09): the cost of a box is split EQUALLY over every unit that comes out
of it. A box costing $110 with 8 promos + 2 packs + 1 sleeve (11 units) gives each unit $10.

Opening N boxes in a warehouse:
  * N boxes leave stock FIFO (box_open: warehouse -> ADJUSTMENT)
  * for every box lot used, each content SKU gets a new lot (box_content: ADJUSTMENT -> warehouse)
    at unit cost = box lot unit cost / units per box, with parent_lot_id -> the box lot
So every card sold later traces back to the box (and purchase batch) it came from.
"""
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.catalog import Sku, sku_box_contents
from app.models.inventory import BoxOpening, InventoryLot
from app.services import audit
from app.services.audit import RequestContext
from app.services.inventory import consume_fifo, location, post_movement


def contents(db: Session, box_sku_id: int) -> list[dict]:
    rows = db.execute(select(sku_box_contents.c.content_sku_id, sku_box_contents.c.qty, Sku.sku_code, Sku.name)
                      .join(Sku, Sku.id == sku_box_contents.c.content_sku_id)
                      .where(sku_box_contents.c.box_sku_id == box_sku_id).order_by(Sku.sku_code)).all()
    return [{"sku_id": r[0], "qty": r[1], "sku_code": r[2], "name": r[3]} for r in rows]


def set_contents(db: Session, *, actor_id: int, box_sku_id: int, version: int, items: list[dict],
                 ctx: RequestContext) -> list[dict]:
    box = db.get(Sku, box_sku_id, with_for_update=True, populate_existing=True)
    if box is None:
        raise AppError("not_found", "SKU not found", 404)
    if box.is_bundle:
        raise AppError("bundle_not_box", "A bundle has no stock and can't be a box; use a normal SKU", 422)
    if box.version != version:
        raise AppError("modified_by_someone_else", "This SKU was changed by another user; reload and retry", 409)
    ids = [i["sku_id"] for i in items]
    if len(ids) != len(set(ids)):
        raise AppError("duplicate_component", "Each content SKU may appear only once", 422)
    if box_sku_id in ids:
        raise AppError("invalid_contents", "A box can't contain itself", 422)
    skus = {s.id: s for s in db.scalars(select(Sku).where(Sku.id.in_(ids)))} if ids else {}
    if missing := [i for i in ids if i not in skus]:
        raise AppError("not_found", "Content SKU not found", 404, {"sku_ids": missing})
    if bundles := [skus[i].sku_code for i in ids if skus[i].is_bundle]:
        raise AppError("invalid_contents", "Bundles can't be box contents", 422, {"sku_codes": bundles})
    if any(i["qty"] <= 0 for i in items):
        raise AppError("invalid_qty", "Quantity must be at least 1", 422)
    before = contents(db, box_sku_id)
    db.execute(delete(sku_box_contents).where(sku_box_contents.c.box_sku_id == box_sku_id))
    if items:
        db.execute(insert(sku_box_contents), [{"box_sku_id": box_sku_id, "content_sku_id": i["sku_id"], "qty": i["qty"]}
                                              for i in items])
    db.execute(update(Sku).where(Sku.id == box_sku_id).values(version=Sku.version + 1))
    after = contents(db, box_sku_id)
    audit.record(db, action="sku.box_contents_changed", entity_type="sku", entity_id=box_sku_id, actor_id=actor_id,
                 before={"contents": before}, after={"contents": after}, ctx=ctx)
    return after


def open_boxes(db: Session, *, actor_id: int, box_sku_id: int, location_code: str, qty: int, idempotency_key: str,
               note: str | None, ctx: RequestContext) -> BoxOpening:
    key = f"boxopen:{idempotency_key}"
    if done := db.scalar(select(BoxOpening).where(BoxOpening.idempotency_key == key)):
        return done
    recipe = contents(db, box_sku_id)
    if not recipe:
        raise AppError("no_box_contents", "Set what is inside this box first", 422)
    if qty <= 0:
        raise AppError("invalid_qty", "Quantity must be at least 1", 422)
    units = sum(r["qty"] for r in recipe)
    loc = location(db, location_code)
    allocs = consume_fifo(db, sku_id=box_sku_id, from_code=location_code, to_code="ADJUSTMENT", movement_type="box_open",
                          qty=qty, key_prefix=f"{key}:out", ref_type="box_opening", actor_id=actor_id, note=note)
    op = BoxOpening(box_sku_id=box_sku_id, location_id=loc.id, qty=qty, units_per_box=units, created_by=actor_id,
                    contents=[{"sku_id": r["sku_id"], "sku_code": r["sku_code"], "qty": r["qty"]} for r in recipe],
                    idempotency_key=key, note=note)
    db.add(op)
    db.flush()
    now = datetime.now(UTC)
    created = []
    for a in allocs:  # one set of content lots per box lot, so costs stay traceable
        per_unit = (a.unit_cost_base / units).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        for r in recipe:
            n = r["qty"] * a.qty
            lot = InventoryLot(sku_id=r["sku_id"], lot_no=f"BX{op.id}-{a.lot_id}-{r['sku_id']}", source="box_open",
                               parent_lot_id=a.lot_id, received_at=now, original_qty=n, original_unit_cost_base=per_unit)
            db.add(lot)
            db.flush()
            post_movement(db, sku_id=r["sku_id"], lot_id=lot.id, movement_type="box_content", from_code="ADJUSTMENT",
                          to_code=location_code, qty=n, key=f"{key}:in:{a.lot_id}:{r['sku_id']}", ref_type="box_opening",
                          ref_id=op.id, actor_id=actor_id, note=note)
            created.append({"sku_code": r["sku_code"], "qty": n, "unit_cost_usd": str(per_unit)})
    audit.record(db, action="inventory.boxes_opened", entity_type="box_opening", entity_id=op.id, actor_id=actor_id,
                 after={"box_sku_id": box_sku_id, "qty": qty, "location": location_code, "units_per_box": units,
                        "created": created}, ctx=ctx)
    return op
