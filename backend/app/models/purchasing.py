"""Purchase batches (historical cost records, usually from the cost spreadsheet) and
purchase orders (drafts created from replenishment suggestions; need human approval).

Once a batch is confirmed/locked its lines cannot be updated or deleted (trigger).
NOTE: purchase_lines cost columns will be finalised in Phase 4 against the owner's
real spreadsheet; this is the initial shape.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in


class PurchaseBatch(IdMixin, TimestampMixin, Base):
    __tablename__ = "purchase_batches"

    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (check_in("status", ("draft", "confirmed", "locked"), "status_valid"),)


class PurchaseLine(IdMixin, CreatedMixin, Base):
    """All *_orig amounts are in currency_orig. fx_rate_to_base converts currency_orig -> USD
    (1 unit of currency_orig = fx_rate_to_base USD). Landed cost is stored in USD."""

    __tablename__ = "purchase_lines"

    batch_id: Mapped[int] = mapped_column(ForeignKey("purchase_batches.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False, index=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_orig: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency_orig: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    fx_rate_to_base: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    fx_source: Mapped[str | None] = mapped_column(String(60))
    domestic_shipping_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    intl_shipping_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    duty_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    other_cost_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    landed_total_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    landed_unit_cost_base: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    landed_cost_method: Mapped[str] = mapped_column(String(20), nullable=False, server_default="computed")
    source_import_row_id: Mapped[int | None] = mapped_column(ForeignKey("import_rows.id"))

    __table_args__ = (
        UniqueConstraint("batch_id", "line_no"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("unit_price_orig >= 0", name="price_nonneg"),
        CheckConstraint("fx_rate_to_base > 0", name="fx_positive"),
        CheckConstraint(
            "domestic_shipping_orig >= 0 AND intl_shipping_orig >= 0 AND duty_orig >= 0 AND other_cost_orig >= 0",
            name="costs_nonneg",
        ),
        CheckConstraint("landed_total_base >= 0 AND landed_unit_cost_base >= 0", name="landed_nonneg"),
        check_in("landed_cost_method", ("computed", "provided"), "method_valid"),
    )


PO_STATUSES = ("draft", "approved", "ordered", "received", "cancelled")


class PurchaseOrder(IdMixin, TimestampMixin, Base):
    """Created as draft only. The system never places orders with suppliers on its own."""

    __tablename__ = "purchase_orders"

    po_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    replenishment_run_id: Mapped[int | None] = mapped_column(ForeignKey("replenishment_runs.id"))
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False, server_default="JPY")
    expected_arrival_date: Mapped[date | None] = mapped_column(Date)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        check_in("status", PO_STATUSES, "status_valid"),
        CheckConstraint(
            "status IN ('draft','cancelled') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_before_ordering",
        ),
    )


class PurchaseOrderLine(IdMixin, Base):
    __tablename__ = "purchase_order_lines"

    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_unit_cost_orig: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    __table_args__ = (
        UniqueConstraint("purchase_order_id", "sku_id"),
        CheckConstraint("qty > 0", name="qty_positive"),
    )
