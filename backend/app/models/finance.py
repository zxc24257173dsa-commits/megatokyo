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
    CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in

INCOME_TYPES = ("revenue", "shipping_income", "other_income")
EXPENSE_TYPES = (
    "cogs", "sales_discount", "platform_fee", "payment_processing_fee", "shipping_cost", "refund",
    "operating_expense", "other_expense",
)
# Signed: late landed-cost corrections; refunds won back on appeal / carrier adjustments (either direction)
# inventory_adjustment: stock written off (damage, count shortage, lost in transit) or found (count surplus)
SIGNED_TYPES = ("cogs_adjustment", "refund_adjustment", "shipping_adjustment", "inventory_adjustment")
ENTRY_TYPES = INCOME_TYPES + EXPENSE_TYPES + SIGNED_TYPES
SOURCE_TYPES = ("api", "webhook", "csv", "manual", "system")


def _sign_rule() -> str:
    inc = ", ".join(f"'{t}'" for t in INCOME_TYPES)
    exp = ", ".join(f"'{t}'" for t in EXPENSE_TYPES)
    return (
        "reversal_of_id IS NOT NULL OR entry_type IN ('cogs_adjustment', 'refund_adjustment', 'shipping_adjustment', 'inventory_adjustment')"
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


class ExpenseCategory(Base):
    """Operating expense categories (configurable; seeded with common ones)."""

    __tablename__ = "expense_categories"

    code: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")


class OperatingExpense(IdMixin, CreatedMixin, Base):
    """One operating expense (rent, staff, packaging...). Booked as an operating_expense ledger entry.
    Never edited: a mistake is voided (reversal entry) and entered again."""

    __tablename__ = "operating_expenses"

    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(ForeignKey("expense_categories.code"), nullable=False)
    amount_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    amount_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    livestream_id: Mapped[int | None] = mapped_column(ForeignKey("livestreams.id"), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    receipt_storage_key: Mapped[str | None] = mapped_column(Text)
    financial_entry_id: Mapped[int | None] = mapped_column(ForeignKey("financial_entries.id"), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    fx_quote: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))  # as entered: 1 USD = fx_quote <currency>
    vendor: Mapped[str | None] = mapped_column(String(120))
    receipt_content_type: Mapped[str | None] = mapped_column(String(60))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    void_reason: Mapped[str | None] = mapped_column(Text)
    void_entry_id: Mapped[int | None] = mapped_column(ForeignKey("financial_entries.id"), unique=True)

    __table_args__ = (
        CheckConstraint("amount_orig > 0 AND amount_base > 0", name="amount_positive"),
        CheckConstraint("fx_rate > 0", name="fx_positive"),
        CheckConstraint("(voided_at IS NULL) = (void_entry_id IS NULL)", name="void_consistent"),
        Index("ix_operating_expenses_date", "expense_date"),
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


REPORT_STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")


class PlatformReportRun(IdMixin, TimestampMixin, Base):
    """A Whatnot report generation run (POST /reports). Generation is asynchronous: the sync
    requests it, polls, and processes the CSV once `completed` (possibly in a later sync)."""

    __tablename__ = "platform_report_runs"

    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="whatnot")
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    report_type: Mapped[str] = mapped_column(String(60), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        check_in("status", REPORT_STATUSES, "status_valid"),
    )


PT_STATUSES = ("booked", "order_not_in_erp", "needs_review")


class PlatformTransaction(IdMixin, TimestampMixin, Base):
    """One row of Whatnot's seller financial report (one ledger transaction): the real commission,
    processing fee, seller-paid shipping, refunds, tips and carrier adjustments.

    Amount columns never change once stored (trigger); a later report with different numbers for
    the same ledger transaction is flagged, not applied. Buyer name / state / country are not stored.
    The booked financial_entries reference it via idempotency keys `whatnot:ledger:<external_id>:*`.
    """

    __tablename__ = "platform_transactions"

    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="whatnot")
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)  # ledger_transaction_id
    transaction_type: Mapped[str] = mapped_column(String(60), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order_placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    sales_order_id: Mapped[int | None] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    external_livestream_id: Mapped[str | None] = mapped_column(String(128))
    livestream_id: Mapped[int | None] = mapped_column(ForeignKey("livestreams.id"), index=True)
    external_shipment_id: Mapped[str | None] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)  # signed net effect on the balance
    buyer_paid: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    original_item_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    coupon_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    post_coupon_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    seller_shipping_charge: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    commission_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    commission_tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    processing_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    processing_fee_tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    quantity_sold: Mapped[int | None] = mapped_column(Integer)
    buy_format: Mapped[str | None] = mapped_column(String(40))
    sale_type: Mapped[str | None] = mapped_column(String(40))
    listing_title: Mapped[str | None] = mapped_column(String(500))
    sku_text: Mapped[str | None] = mapped_column(String(128))
    message: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    report_run_id: Mapped[int | None] = mapped_column(ForeignKey("platform_report_runs.id"))
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    upstream_changed: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        check_in("status", PT_STATUSES, "status_valid"),
        check_in("source", ("api", "csv"), "source_valid"),
        Index("ix_platform_transactions_status", "status", postgresql_where="status <> 'booked'"),
    )
