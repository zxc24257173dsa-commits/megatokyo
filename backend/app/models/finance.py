"""Financial ledger.

financial_entries is append-only. Amounts are SIGNED from the P&L point of view:
income types are >= 0, cost/expense types are <= 0, so  profit = SUM(amount_base).
A correction is a reversal entry (reversal_of_id set, opposite sign) plus a new entry.
Every entry carries its data source; fees only come from real data (API / Whatnot export / manual).
Base currency is USD.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin
from app.models._util import check_in

INCOME_TYPES = ("revenue", "shipping_income", "other_income")
EXPENSE_TYPES = (
    "cogs", "sales_discount", "platform_fee", "payment_processing_fee", "shipping_cost", "refund",
    "operating_expense", "other_expense",
)
SIGNED_TYPES = ("cogs_adjustment",)  # late landed-cost corrections can go either way
ENTRY_TYPES = INCOME_TYPES + EXPENSE_TYPES + SIGNED_TYPES
SOURCE_TYPES = ("api", "webhook", "csv", "manual", "system")


def _sign_rule() -> str:
    inc = ", ".join(f"'{t}'" for t in INCOME_TYPES)
    exp = ", ".join(f"'{t}'" for t in EXPENSE_TYPES)
    return (
        "reversal_of_id IS NOT NULL OR entry_type = 'cogs_adjustment'"
        f" OR (entry_type IN ({inc}) AND amount_base >= 0)"
        f" OR (entry_type IN ({exp}) AND amount_base <= 0)"
    )


class FinancialEntry(IdMixin, CreatedMixin, Base):
    __tablename__ = "financial_entries"

    entry_date: Mapped[date] = mapped_column(Date, nullable=False)  # business date, America/New_York
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency_orig: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)  # 1 currency_orig = fx_rate USD
    amount_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    sales_order_id: Mapped[int | None] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    order_line_id: Mapped[int | None] = mapped_column(ForeignKey("sales_order_lines.id"), index=True)
    livestream_id: Mapped[int | None] = mapped_column(ForeignKey("livestreams.id"))
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"))
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"))
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255))
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("financial_entries.id"), unique=True)
    memo: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        check_in("entry_type", ENTRY_TYPES, "type_valid"),
        check_in("source_type", SOURCE_TYPES, "source_valid"),
        CheckConstraint("fx_rate > 0", name="fx_positive"),
        CheckConstraint("amount_orig * amount_base >= 0", name="same_sign"),
        CheckConstraint(_sign_rule(), name="sign_by_type"),
        Index("ix_financial_entries_livestream", "livestream_id", "entry_type"),
        Index("ix_financial_entries_date_type", "entry_date", "entry_type"),
        Index("ix_financial_entries_sku_date", "sku_id", "entry_date"),
    )


class Payout(IdMixin, CreatedMixin, Base):
    """Actual settlement from Whatnot. Shown next to (never confused with) profit."""

    __tablename__ = "payouts"

    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="whatnot")
    external_payout_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"))

    __table_args__ = (
        UniqueConstraint("platform", "external_payout_id"),
        check_in("source", ("api", "csv", "manual"), "source_valid"),
    )


class OperatingExpense(IdMixin, CreatedMixin, Base):
    __tablename__ = "operating_expenses"

    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    amount_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    amount_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    livestream_id: Mapped[int | None] = mapped_column(ForeignKey("livestreams.id"), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    receipt_storage_key: Mapped[str | None] = mapped_column(Text)
    financial_entry_id: Mapped[int | None] = mapped_column(ForeignKey("financial_entries.id"), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        CheckConstraint("amount_orig > 0 AND amount_base > 0", name="amount_positive"),
        CheckConstraint("fx_rate > 0", name="fx_positive"),
    )


class AccountingPeriod(Base):
    """Closed months reject new financial entries (trigger); corrections go to an open period."""

    __tablename__ = "accounting_periods"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # 'YYYY-MM'
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")
    closed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        check_in("status", ("open", "closed"), "status_valid"),
        CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="period_format"),
    )
