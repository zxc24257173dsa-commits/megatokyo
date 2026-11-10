"""/api/v1 replenishment (Phase 10). Suggestions only - the system never orders anything by itself."""
import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_request_context, require_permissions
from app.db.session import get_db
from app.models.replenishment import ReplenishmentRun
from app.services import replenishment as R
from app.services.audit import RequestContext

router = APIRouter(tags=["replenishment"])
view = require_permissions("replenishment.view")
edit = require_permissions("replenishment.edit")


@router.get("/replenishment/runs")
def runs(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return [{"id": r.id, "data_as_of": r.data_as_of, "created_at": r.created_at, "note": r.note}
            for r in db.scalars(select(ReplenishmentRun).order_by(ReplenishmentRun.id.desc()).limit(30))]


@router.get("/replenishment/runs/latest")
def latest(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.run_detail(db)


@router.get("/replenishment/runs/{run_id}")
def run(run_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.run_detail(db, run_id)


class RunIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/replenishment/runs", status_code=201)
def create_run(body: RunIn, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
               ctx: RequestContext = Depends(get_request_context)):
    r = R.create_run(db, actor_id=current.id, note=body.note, ctx=ctx)
    db.commit()
    return R.run_detail(db, r.id)


class OverrideIn(BaseModel):
    qty: int | None = Field(default=None, ge=0, le=1_000_000)
    reason: str | None = Field(default=None, max_length=500)


@router.patch("/replenishment/suggestions/{suggestion_id}")
def override(suggestion_id: int, body: OverrideIn, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
             ctx: RequestContext = Depends(get_request_context)):
    s = R.override(db, actor_id=current.id, suggestion_id=suggestion_id, qty=body.qty, reason=body.reason, ctx=ctx)
    db.commit()
    return {"id": s.id, "override_purchase_qty": s.override_purchase_qty, "override_reason": s.override_reason}


class ConfirmIn(BaseModel):
    suggestion_ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/replenishment/runs/{run_id}/confirm")
def confirm(run_id: int, body: ConfirmIn, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
            ctx: RequestContext = Depends(get_request_context)):
    n = R.confirm(db, actor_id=current.id, run_id=run_id, suggestion_ids=body.suggestion_ids, ctx=ctx)
    db.commit()
    return {"confirmed": n}


@router.get("/replenishment/runs/{run_id}/purchase-list.csv")
def purchase_list(run_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    d = R.run_detail(db, run_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SKU", "Product", "Quantity to buy", "Suggested", "Changed because", "Latest order date",
                "US available", "In transit", "Purchased not shipped", "Demand per day", "Safety stock", "Confirmed"])
    for s in d["suggestions"]:
        if s["action"] != "buy" and not s["override_purchase_qty"]:
            continue
        w.writerow([s["sku_code"], s["name"], s["final_qty"], s["suggested_purchase_qty"], s["override_reason"] or "",
                    s["latest_order_date"] or "", s["us_available"], s["in_transit_in_time"] + s["in_transit_late"],
                    s["on_order"], round(s["v_blended"], 2), s["safety_stock"], "yes" if s["confirmed_at"] else ""])
    name = f"megatokyo-purchase-list-{d['run']['data_as_of']}.csv"
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/replenishment/settings")
def get_settings(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.settings_out(R.settings(db))


class SettingsIn(BaseModel):
    supplier_lead_time_days: int | None = Field(default=None, ge=1, le=120)
    review_period_days: int | None = Field(default=None, ge=1, le=90)
    service_level_z: Decimal | None = Field(default=None, ge=0, le=4)
    weights: list[Decimal] | None = None
    min_history_days: int | None = Field(default=None, ge=1, le=90)


@router.put("/replenishment/settings")
def put_settings(body: SettingsIn, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    st = R.update_settings(db, actor_id=current.id, values=body.model_dump(), ctx=ctx)
    db.commit()
    return R.settings_out(st)


class SkuParamsIn(BaseModel):
    excluded: bool | None = None
    case_pack: int | None = Field(default=None, ge=1, le=10000)
    min_ship_qty: int | None = Field(default=None, ge=0, le=100000)
    supplier_lead_time_days: int | None = Field(default=None, ge=1, le=120)
    manual_daily_velocity: Decimal | None = Field(default=None, ge=0, le=100000)
    safety_stock_override: int | None = Field(default=None, ge=0, le=100000)


@router.put("/replenishment/skus/{sku_id}")
def sku_params(sku_id: int, body: SkuParamsIn, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
               ctx: RequestContext = Depends(get_request_context)):
    from app.services.catalog import get_sku
    get_sku(db, sku_id)
    p = R.set_sku_params(db, actor_id=current.id, sku_id=sku_id, values=body.model_dump(exclude_unset=True), ctx=ctx)
    db.commit()
    return {"sku_id": sku_id, "excluded": p.excluded, "case_pack": p.case_pack, "min_ship_qty": p.min_ship_qty,
            "supplier_lead_time_days": p.supplier_lead_time_days,
            "manual_daily_velocity": float(p.manual_daily_velocity) if p.manual_daily_velocity is not None else None,
            "safety_stock_override": p.safety_stock_override}
