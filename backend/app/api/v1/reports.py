"""Reports: per-show P&L (Phase 6). Read-only; every figure comes from the ledger."""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_permissions
from app.db.session import get_db
from app.services import livestream_report as R
from app.services import trends as T

router = APIRouter(tags=["reports"])
view = require_permissions("finance.view_livestream")
ops = require_permissions("orders.view")  # trends: units for everyone who sees orders, money only with finance access


def _money(current: CurrentUser) -> bool:
    return bool({"finance.view", "finance.view_livestream"} & set(current.permissions))


@router.get("/reports/livestreams")
def livestreams(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.list_shows(db, date_from, date_to)


@router.get("/reports/livestreams/{livestream_id}")
def livestream(livestream_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.show_detail(db, livestream_id)


@router.get("/reports/trends")
def trends(date_from: date = Query(..., alias="from"), date_to: date = Query(..., alias="to"),
           granularity: str = "week", group: str = "sku", current: CurrentUser = Depends(ops),
           db: Session = Depends(get_db)):
    return T.overview(db, date_from, date_to, granularity, group, with_money=_money(current))


@router.get("/reports/trends/movers")
def movers(weeks: int = 4, current: CurrentUser = Depends(ops), db: Session = Depends(get_db)):
    return T.movers(db, weeks, with_money=_money(current))


@router.get("/reports/trends/skus/{sku_id}")
def sku_demand(sku_id: int, days: int = 90, current: CurrentUser = Depends(ops), db: Session = Depends(get_db)):
    return T.sku_demand(db, sku_id, days)
