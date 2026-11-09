"""Livestreams (Whatnot shows), sales orders and lines, refunds, COGS allocations.

Owner rule (2026-11-09): an order counts as a sale when the buyer has paid; cancellations
do not occur in this business. Revenue is recognised at paid_at.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in

DATA_SOURCES = ("api", "webhook", "csv", "manual")


class Livestream(IdMixin, TimestampMixin, Base):
    __tablename__ = "livestreams"

    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="whatnot")
    external_id: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="scheduled")
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        check_in("status", ("scheduled", "live", "ended", "cancelled"), "status_valid"),
        check_in("source", DATA_SOURCES, "source_valid"),
        Index("ix_livestreams_started_at", "started_at"),
    )


class LivestreamMetric(IdMixin, CreatedMixin, Base):
    """Only metrics a real source provides (API, Whatnot export, or manual entry)."""

    __tablename__ = "livestream_metrics"

    livestream_id: Mapped[int] = mapped_column(ForeignKey("livestreams.id", ondelete="CASCADE"), nullable=False)
    metric_code: Mapped[str] = mapped_column(String(60), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    import_job_id: Mapped[int | None] = mapped_column(ForeignKey("import_jobs.id"))

    __table_args__ = (
        UniqueConstraint("livestream_id", "metric_code", "source"),
        check_in("source", DATA_SOURCES, "source_valid"),
    )


class SalesOrder(IdMixin, TimestampMixin, Base):
    __tablename__ = "sales_orders"

    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="whatnot")
    external_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    livestream_id: Mapped[int | None] = mapped_column(ForeignKey("livestreams.id"), index=True)
    buyer_ref_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False, server_default="USD")
    item_subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    shipping_charged: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    sales_tax_collected: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    total: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("platform", "external_order_id"),
        check_in("source", DATA_SOURCES, "source_valid"),
        CheckConstraint("item_subtotal >= 0 AND shipping_charged >= 0 AND sales_tax_collected >= 0", name="amounts_nonneg"),
        Index("ix_sales_orders_paid_at", "paid_at"),
    )


class SalesOrderLine(IdMixin, TimestampMixin, Base):
    __tablename__ = "sales_order_lines"

    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), nullable=False)
    external_line_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"), index=True)
    external_product_ref: Mapped[str | None] = mapped_column(String(255))
    title_snapshot: Mapped[str | None] = mapped_column(String(500))
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    discount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    line_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fulfillment_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unfulfilled")
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mapping_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="unmapped")

    __table_args__ = (
        UniqueConstraint("order_id", "external_line_id"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("unit_price >= 0 AND discount >= 0 AND line_amount >= 0", name="amounts_nonneg"),
        check_in("fulfillment_status", ("unfulfilled", "shipped", "delivered"), "fulfillment_valid"),
        check_in("mapping_status", ("mapped", "unmapped"), "mapping_valid"),
        CheckConstraint("(mapping_status = 'mapped') = (sku_id IS NOT NULL)", name="mapping_consistent"),
    )


class OrderStatusHistory(IdMixin, Base):
    """Append-only."""

    __tablename__ = "order_status_history"

    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("webhook_events.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Refund(IdMixin, CreatedMixin, Base):
    __tablename__ = "refunds"

    platform: Mapped[str] = mapped_column(String(30), nullable=False, server_default="whatnot")
    external_refund_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), nullable=False, index=True)
    order_line_id: Mapped[int | None] = mapped_column(ForeignKey("sales_order_lines.id"))
    amount_orig: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    refunded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    restocked: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        UniqueConstraint("platform", "external_refund_id"),
        CheckConstraint("amount_orig > 0", name="amount_positive"),
        check_in("source", DATA_SOURCES, "source_valid"),
    )


class CogsAllocation(IdMixin, CreatedMixin, Base):
    """Append-only. Which lot(s) each sold unit came from, at what USD unit cost.
    For a bundle line there is one set of rows per component SKU."""

    __tablename__ = "cogs_allocations"

    order_line_id: Mapped[int] = mapped_column(ForeignKey("sales_order_lines.id"), nullable=False, index=True)
    component_sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id"), nullable=False, index=True)
    movement_id: Mapped[int] = mapped_column(ForeignKey("inventory_movements.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost_base: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    total_cost_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)

    __table_args__ = (
        UniqueConstraint("order_line_id", "lot_id", "movement_id"),
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("unit_cost_base >= 0 AND total_cost_base >= 0", name="cost_nonneg"),
    )
