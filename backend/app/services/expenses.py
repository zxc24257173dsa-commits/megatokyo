"""Operating expenses and accounting periods (Phase 7).

An expense is entered in its own currency. For JPY / TWD the user enters the rate the same way as
on the cost sheet, "1 USD = N" (e.g. 150 JPY); the USD amount = amount / N. Each expense books one
operating_expense ledger entry (negative). It is never edited: a mistake is voided (reversal entry,
reason required) and entered again, so the history stays visible.
"""
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.finance import AccountingPeriod, ExpenseCategory, FinancialEntry, OperatingExpense
from app.models.sales import Livestream
from app.services import audit, ledger, storage
from app.services.audit import RequestContext

CURRENCIES = ("USD", "JPY", "TWD")
MAX_RECEIPT_BYTES = 10 * 1024 * 1024


def sniff_receipt(data: bytes) -> tuple[str, str]:
    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    try:
        return storage.sniff_image(data)  # JPEG / PNG / WebP, by content not by name
    except AppError:
        raise AppError("unsupported_file_type", "Receipts must be a photo (JPEG, PNG, WebP) or a PDF", 415) from None


def _q4(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def create(db: Session, *, actor_id: int, expense_date: date, category: str, amount: Decimal, currency: str,
           fx_quote: Decimal | None, vendor: str | None, description: str | None, livestream_id: int | None,
           receipt: bytes | None, idempotency_key: str | None, ctx: RequestContext) -> OperatingExpense:
    if idempotency_key:
        done = db.scalar(select(OperatingExpense).where(OperatingExpense.idempotency_key == idempotency_key))
        if done:
            return done  # double tap on "save"
    cat = db.get(ExpenseCategory, category)
    if cat is None or not cat.is_active:
        raise AppError("invalid_category", "Unknown expense category", 422)
    currency = currency.upper()
    if currency not in CURRENCIES:
        raise AppError("invalid_currency", "Currency must be USD, JPY or TWD", 422)
    if amount <= 0:
        raise AppError("invalid_amount", "Amount must be greater than 0", 422)
    if currency == "USD":
        fx_quote = None
        fx_rate, base = Decimal(1), _q4(amount)
    else:
        if not fx_quote or fx_quote <= 0:
            raise AppError("fx_required", f"Enter the exchange rate: 1 USD = ? {currency}", 422)
        fx_rate = (Decimal(1) / fx_quote).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        base = _q4(amount / fx_quote)
        if base <= 0:
            raise AppError("invalid_amount", "Amount is too small", 422)
    if ledger.is_closed(db, expense_date):
        raise AppError("period_closed", f"{ledger.period_of(expense_date)} 已結帳，不能再記入費用；請記在目前的月份", 409,
                       {"period": ledger.period_of(expense_date)})
    if expense_date > ledger.biz_date(datetime.now(UTC)):
        raise AppError("future_date", "Expense date cannot be in the future", 422)
    if livestream_id is not None and db.get(Livestream, livestream_id) is None:
        raise AppError("not_found", "Livestream not found", 404)
    key = ctype = None
    if receipt:
        if len(receipt) > MAX_RECEIPT_BYTES:
            raise AppError("file_too_large", "Receipt must be 10 MB or smaller", 413)
        ctype, ext = sniff_receipt(receipt)
        key = storage.save("receipts", receipt, ext)

    exp = OperatingExpense(expense_date=expense_date, category=category, amount_orig=_q4(amount), currency=currency,
                           fx_rate=fx_rate, fx_quote=fx_quote, amount_base=base, livestream_id=livestream_id,
                           vendor=(vendor or "").strip()[:120] or None, description=(description or "").strip() or None,
                           receipt_storage_key=key, receipt_content_type=ctype, idempotency_key=idempotency_key,
                           created_by=actor_id)
    db.add(exp)
    db.flush()
    when = datetime.combine(expense_date, datetime.min.time(), UTC)
    entry = FinancialEntry(
        idempotency_key=f"expense:{exp.id}", entry_date=expense_date, occurred_at=when, entry_type="operating_expense",
        amount_orig=-exp.amount_orig, currency_orig=currency, fx_rate=fx_rate, amount_base=-base,
        livestream_id=livestream_id, source_type="manual", source_ref=f"expense:{exp.id}",
        memo=f"{cat.name}{'：' + exp.description if exp.description else ''}"[:500], created_by=actor_id)
    db.add(entry)
    db.flush()
    exp.financial_entry_id = entry.id
    db.flush()
    audit.record(db, action="expense.created", entity_type="operating_expense", entity_id=exp.id, actor_id=actor_id,
                 after={"date": str(expense_date), "category": category, "amount": str(exp.amount_orig),
                        "currency": currency, "usd": str(base), "livestream_id": livestream_id}, ctx=ctx)
    return exp


def void(db: Session, *, actor_id: int, expense_id: int, reason: str, ctx: RequestContext) -> OperatingExpense:
    exp = db.get(OperatingExpense, expense_id, with_for_update=True, populate_existing=True)
    if exp is None:
        raise AppError("not_found", "Expense not found", 404)
    if exp.voided_at is not None:
        raise AppError("already_voided", "This expense is already voided", 409)
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise AppError("reason_required", "Give a reason for voiding", 422)
    orig = db.get(FinancialEntry, exp.financial_entry_id)
    day, moved = ledger.booking_date(db, exp.expense_date)
    rev = FinancialEntry(
        idempotency_key=f"expense:{exp.id}:void", entry_date=day, occurred_at=datetime.now(UTC),
        entry_type="operating_expense", amount_orig=-orig.amount_orig, currency_orig=orig.currency_orig,
        fx_rate=orig.fx_rate, amount_base=-orig.amount_base, livestream_id=orig.livestream_id, source_type="manual",
        source_ref=orig.source_ref, reversal_of_id=orig.id,
        memo="；".join(x for x in (f"作廢：{reason}", moved) if x)[:500], created_by=actor_id)
    db.add(rev)
    db.flush()
    exp.voided_at, exp.voided_by, exp.void_reason, exp.void_entry_id = datetime.now(UTC), actor_id, reason[:500], rev.id
    db.flush()
    audit.record(db, action="expense.voided", entity_type="operating_expense", entity_id=exp.id, actor_id=actor_id,
                 after={"reason": reason, "booked_on": str(day)}, ctx=ctx)
    return exp


def to_out(db: Session, e: OperatingExpense, names: dict[str, str]) -> dict:
    ls = db.get(Livestream, e.livestream_id) if e.livestream_id else None
    return {"id": e.id, "expense_date": e.expense_date, "category": e.category, "category_name": names.get(e.category),
            "amount": float(e.amount_orig), "currency": e.currency, "fx_quote": float(e.fx_quote) if e.fx_quote else None,
            "amount_usd": float(e.amount_base), "vendor": e.vendor, "description": e.description,
            "livestream_id": e.livestream_id, "livestream_title": ls.title if ls else None,
            "receipt_url": storage.signed_url(e.receipt_storage_key) if e.receipt_storage_key else None,
            "receipt_content_type": e.receipt_content_type, "created_by": e.created_by, "created_at": e.created_at,
            "voided_at": e.voided_at, "void_reason": e.void_reason}


# ---------- accounting periods ----------

def set_period(db: Session, *, actor_id: int, period: str, close: bool, ctx: RequestContext) -> AccountingPeriod:
    try:
        first = date.fromisoformat(period + "-01")
    except ValueError:
        raise AppError("invalid_period", "Period must be YYYY-MM", 422) from None
    today = ledger.biz_date(datetime.now(UTC))
    if close and (first.year, first.month) >= (today.year, today.month):
        raise AppError("period_not_over", "Only a finished month can be closed", 409)
    row = db.get(AccountingPeriod, period, with_for_update=True)
    if row is None:
        row = AccountingPeriod(period=period, status="open")
        db.add(row)
    before = row.status
    row.status = "closed" if close else "open"
    row.closed_by, row.closed_at = (actor_id, datetime.now(UTC)) if close else (None, None)
    row.updated_at = datetime.now(UTC)
    db.flush()
    audit.record(db, action="period.closed" if close else "period.reopened", entity_type="accounting_period",
                 entity_id=period, actor_id=actor_id, before={"period": period, "status": before},
                 after={"period": period, "status": row.status}, ctx=ctx)
    return row
