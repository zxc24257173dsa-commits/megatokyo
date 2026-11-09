"""/api/v1/inventory: positions, summary, movements, opening stock, adjustments, stock counts.

Write endpoints require an `Idempotency-Key` header (generated once per form submission by the
frontend). Repeating the same request - e.g. a double tap on a slow phone connection in the
warehouse - returns the original result and never moves stock twice.
"""
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_request_context, require_permissions
from app.db.session import get_db
from app.models.catalog import Sku
from app.models.inventory import StockCount, StockCountLine
from app.models.reference import Location
from app.services import inventory as svc
from app.services import inventory_queries as q
from app.services.audit import RequestContext

router = APIRouter(prefix="/inventory", tags=["inventory"])
view = require_permissions("inventory.view")
IdemKey = Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
StockLocation = Literal["JP_MAIN", "US_MAIN"]


class Line(BaseModel):
    sku_id: int
    qty: int = Field(ge=1, le=1_000_000)
    unit_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6)


class OpeningStockIn(BaseModel):
    location_code: StockLocation
    note: str | None = Field(default=None, max_length=500)
    lines: list[Line] = Field(min_length=1, max_length=500)


class AdjustmentIn(BaseModel):
    location_code: StockLocation
    kind: Literal["increase", "decrease", "damage"]
    reason: str = Field(min_length=1, max_length=500)
    lines: list[Line] = Field(min_length=1, max_length=500)


class MovementResult(BaseModel):
    movement_ids: list[int]


@router.get("/summary")
def summary(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return {"locations": q.inventory_summary(db, with_cost=q.can_view_cost(current.permissions)),
            "can_view_cost": q.can_view_cost(current.permissions)}


@router.get("/movements")
def movements(sku_id: int | None = None, location_code: str | None = None, movement_type: str | None = None,
              before_id: int | None = None, limit: int = Query(default=50, ge=1, le=200),
              _: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    items, nxt = q.movements(db, sku_id=sku_id, location_code=location_code, movement_type=movement_type,
                             before_id=before_id, limit=limit)
    return {"items": items, "next_before_id": nxt}


@router.post("/opening-stock", response_model=MovementResult, status_code=201)
def opening_stock(body: OpeningStockIn, idempotency_key: str = IdemKey, current: CurrentUser = Depends(view),
                  db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    svc.require_location_permission(body.location_code, current.permissions)
    for i, line in enumerate(body.lines):
        if line.unit_cost_usd is None:
            from app.core.errors import AppError
            raise AppError("unit_cost_required", "Opening stock needs a unit cost (USD) on every line", 422,
                           {"line": i + 1})
    moves = svc.opening_stock(db, actor_id=current.id, location_code=body.location_code,
                              lines=[l.model_dump() for l in body.lines], idempotency_key=idempotency_key,
                              note=body.note, ctx=ctx)
    db.commit()
    return MovementResult(movement_ids=[m.id for m in moves])


@router.post("/adjustments", response_model=MovementResult, status_code=201)
def adjust(body: AdjustmentIn, idempotency_key: str = IdemKey, current: CurrentUser = Depends(view),
           db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    svc.require_location_permission(body.location_code, current.permissions)
    moves = svc.adjust(db, actor_id=current.id, location_code=body.location_code, kind=body.kind, reason=body.reason,
                       lines=[l.model_dump() for l in body.lines], idempotency_key=idempotency_key, ctx=ctx)
    db.commit()
    return MovementResult(movement_ids=[m.id for m in moves])


# ---------- stock counts ----------

class CountCreate(BaseModel):
    location_code: StockLocation
    sku_ids: list[int] | None = None
    note: str | None = Field(default=None, max_length=500)


class CountEntry(BaseModel):
    sku_id: int
    counted_qty: int | None = Field(default=None, ge=0)


class CountEntries(BaseModel):
    counts: list[CountEntry] = Field(min_length=1, max_length=1000)


def _count_out(db: Session, sc: StockCount) -> dict:
    loc = db.get(Location, sc.location_id)
    lines = db.execute(
        select(StockCountLine, Sku.sku_code, Sku.name).join(Sku, Sku.id == StockCountLine.sku_id)
        .where(StockCountLine.stock_count_id == sc.id).order_by(Sku.sku_code)
    ).all()
    return {
        "id": sc.id, "location_code": loc.code, "status": sc.status, "note": sc.note, "created_at": sc.created_at,
        "submitted_at": sc.submitted_at, "approved_at": sc.approved_at,
        "lines": [{"sku_id": l.sku_id, "sku_code": code, "name": name, "expected_qty": l.expected_qty,
                   "counted_qty": l.counted_qty, "difference_qty": l.difference_qty} for l, code, name in lines],
    }


def _count_permission(db: Session, count_id: int, current: CurrentUser) -> None:
    sc = svc.get_count(db, count_id)
    svc.require_location_permission(db.get(Location, sc.location_id).code, current.permissions)


@router.get("/stock-counts")
def list_counts(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    rows = db.execute(select(StockCount, Location.code).join(Location, Location.id == StockCount.location_id)
                      .order_by(StockCount.id.desc()).limit(100)).all()
    return [{"id": sc.id, "location_code": code, "status": sc.status, "created_at": sc.created_at,
             "approved_at": sc.approved_at, "note": sc.note} for sc, code in rows]


@router.post("/stock-counts", status_code=201)
def create_count(body: CountCreate, current: CurrentUser = Depends(view), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    svc.require_location_permission(body.location_code, current.permissions)
    sc = svc.create_count(db, actor_id=current.id, location_code=body.location_code, sku_ids=body.sku_ids,
                          note=body.note, ctx=ctx)
    db.commit()
    return _count_out(db, sc)


@router.get("/stock-counts/{count_id}")
def get_count(count_id: int, _: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return _count_out(db, svc.get_count(db, count_id))


@router.put("/stock-counts/{count_id}/counts")
def record_counts(count_id: int, body: CountEntries, current: CurrentUser = Depends(view),
                  db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    _count_permission(db, count_id, current)
    sc = svc.record_counts(db, actor_id=current.id, count_id=count_id, counts=[c.model_dump() for c in body.counts],
                           ctx=ctx)
    db.commit()
    return _count_out(db, sc)


@router.post("/stock-counts/{count_id}/submit")
def submit_count(count_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    _count_permission(db, count_id, current)
    sc = svc.submit_count(db, actor_id=current.id, count_id=count_id, ctx=ctx)
    db.commit()
    return _count_out(db, sc)


@router.post("/stock-counts/{count_id}/approve")
def approve_count(count_id: int, current: CurrentUser = Depends(require_permissions("stock_count.approve")),
                  db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    sc = svc.approve_count(db, actor_id=current.id, count_id=count_id, ctx=ctx)
    db.commit()
    return _count_out(db, sc)


@router.post("/stock-counts/{count_id}/cancel")
def cancel_count(count_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    _count_permission(db, count_id, current)
    sc = svc.cancel_count(db, actor_id=current.id, count_id=count_id, ctx=ctx)
    db.commit()
    return _count_out(db, sc)
