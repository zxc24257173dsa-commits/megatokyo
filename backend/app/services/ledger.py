"""Shared ledger rules: closed accounting periods.

A closed month (accounting_periods.status = 'closed') rejects new financial entries (DB trigger).
Automatic bookings (orders, shipments, Whatnot fees) that belong to a closed month are booked on
today's business date instead, with a memo naming the original month - the closed month's figures
never change, and nothing is lost. Manual entries (expenses) into a closed month are refused.
"""
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings


def biz_date(dt: datetime) -> date:
    return dt.astimezone(ZoneInfo(get_settings().business_timezone)).date()


def period_of(d: date) -> str:
    return d.strftime("%Y-%m")


def is_closed(db: Session, d: date) -> bool:
    return bool(db.scalar(text("SELECT 1 FROM accounting_periods WHERE period = :p AND status = 'closed'"),
                          {"p": period_of(d)}))


def booking_date(db: Session, d: date) -> tuple[date, str | None]:
    """(date to book on, memo) - today when `d` falls in a closed month."""
    if not is_closed(db, d):
        return d, None
    today = biz_date(datetime.now(UTC))
    return today, f"屬於已結帳的 {period_of(d)}，記入 {period_of(today)}"
