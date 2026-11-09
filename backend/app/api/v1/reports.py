"""Reports: per-show P&L (Phase 6). Read-only; every figure comes from the ledger."""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, require_permissions
from app.db.session import get_db
from app.services import livestream_report as R

router = APIRouter(tags=["reports"])
view = require_permissions("finance.view_livestream")


@router.get("/reports/livestreams")
def livestreams(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.list_shows(db, date_from, date_to)


@router.get("/reports/livestreams/{livestream_id}")
def livestream(livestream_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return R.show_detail(db, livestream_id)
