"""/api/v1 finance (Phase 7): operating expenses, P&L, ledger export, accounting periods.

Expenses: `expense.create` records; people with `finance.view` see everyone's expenses, others only
their own (salaries etc. stay private). P&L, export and periods need `finance.view`; closing or
reopening a month needs `period.close`.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_active_user, get_request_context, require_permissions
from app.core.errors import AppError
from app.db.session import get_db
from app.models.finance import AccountingPeriod, ExpenseCategory, OperatingExpense
from app.services import expenses as X
from app.services import ledger, pnl
from app.services.audit import RequestContext

router = APIRouter(tags=["finance"])
view = require_permissions("finance.view")
record = require_permissions("expense.create")
close = require_permissions("period.close")


def _expense_reader(current: CurrentUser = Depends(get_active_user)) -> CurrentUser:
    if not {"finance.view", "expense.create"} & set(current.permissions):
        raise AppError("forbidden", "You do not have permission for this action", 403,
                       {"missing": ["expense.create"]})
    return current


def _names(db: Session) -> dict[str, str]:
    return dict(db.execute(select(ExpenseCategory.code, ExpenseCategory.name)).all())


@router.get("/expense-categories")
def categories(current: CurrentUser = Depends(_expense_reader), db: Session = Depends(get_db)):
    rows = db.scalars(select(ExpenseCategory).where(ExpenseCategory.is_active)
                      .order_by(ExpenseCategory.sort_order, ExpenseCategory.code))
    return [{"code": c.code, "name": c.name} for c in rows]


@router.get("/expenses")
def list_expenses(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                  category: str | None = None, include_voided: bool = True,
                  current: CurrentUser = Depends(_expense_reader), db: Session = Depends(get_db)):
    today = ledger.biz_date(datetime.now(UTC))
    date_to = date_to or today
    date_from = date_from or (date_to - timedelta(days=90))
    q = select(OperatingExpense).where(OperatingExpense.expense_date.between(date_from, date_to))
    if "finance.view" not in current.permissions:
        q = q.where(OperatingExpense.created_by == current.id)
    if category:
        q = q.where(OperatingExpense.category == category)
    if not include_voided:
        q = q.where(OperatingExpense.voided_at.is_(None))
    names = _names(db)
    rows = db.scalars(q.order_by(OperatingExpense.expense_date.desc(), OperatingExpense.id.desc()).limit(1000))
    return [X.to_out(db, e, names) for e in rows]


def _dec(v: str | None, field: str) -> Decimal | None:
    if v is None or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).replace(",", "").strip())
    except InvalidOperation:
        raise AppError("invalid_number", f"{field} is not a number", 422) from None


@router.post("/expenses", status_code=201)
async def create_expense(
    expense_date: date = Form(...), category: str = Form(...), amount: str = Form(...), currency: str = Form("USD"),
    fx_quote: str | None = Form(None), vendor: str | None = Form(None, max_length=120),
    description: str | None = Form(None, max_length=2000), livestream_id: int | None = Form(None),
    receipt: UploadFile | None = File(None),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$"),
    current: CurrentUser = Depends(record), db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
):
    data = await receipt.read(X.MAX_RECEIPT_BYTES + 1) if receipt is not None and receipt.filename else None
    e = X.create(db, actor_id=current.id, expense_date=expense_date, category=category,
                 amount=_dec(amount, "amount"), currency=currency, fx_quote=_dec(fx_quote, "fx_quote"),
                 vendor=vendor, description=description, livestream_id=livestream_id, receipt=data,
                 idempotency_key=f"{current.id}:{idempotency_key}", ctx=ctx)
    db.commit()
    return X.to_out(db, e, _names(db))


class VoidIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


@router.post("/expenses/{expense_id}/void")
def void_expense(expense_id: int, body: VoidIn, current: CurrentUser = Depends(_expense_reader),
                 db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    e = db.get(OperatingExpense, expense_id)
    if e is None or ("finance.view" not in current.permissions and e.created_by != current.id):
        raise AppError("not_found", "Expense not found", 404)
    e = X.void(db, actor_id=current.id, expense_id=expense_id, reason=body.reason, ctx=ctx)
    db.commit()
    return X.to_out(db, e, _names(db))


@router.get("/reports/pnl")
def pnl_report(date_from: date = Query(..., alias="from"), date_to: date = Query(..., alias="to"),
               granularity: str = "month", current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return pnl.report(db, date_from, date_to, granularity)


@router.get("/reports/pnl/entries.csv")
def pnl_entries(date_from: date = Query(..., alias="from"), date_to: date = Query(..., alias="to"),
                current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    if date_to < date_from or (date_to - date_from).days > 800:
        raise AppError("invalid_range", "Choose a range of at most about two years", 422)
    name = f"megatokyo-ledger-{date_from}-{date_to}.csv"
    return StreamingResponse(pnl.entries_csv(db, date_from, date_to), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/accounting-periods")
def periods(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    rows = db.scalars(select(AccountingPeriod).order_by(AccountingPeriod.period.desc()))
    return [{"period": p.period, "status": p.status, "closed_at": p.closed_at, "closed_by": p.closed_by} for p in rows]


@router.post("/accounting-periods/{period}/close")
def close_period(period: str, current: CurrentUser = Depends(close), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    p = X.set_period(db, actor_id=current.id, period=period, close=True, ctx=ctx)
    db.commit()
    return {"period": p.period, "status": p.status}


@router.post("/accounting-periods/{period}/reopen")
def reopen_period(period: str, current: CurrentUser = Depends(close), db: Session = Depends(get_db),
                  ctx: RequestContext = Depends(get_request_context)):
    p = X.set_period(db, actor_id=current.id, period=period, close=False, ctx=ctx)
    db.commit()
    return {"period": p.period, "status": p.status}
