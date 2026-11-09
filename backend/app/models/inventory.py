"""Inventory ledger.

inventory_movements   append-only double-entry ledger (from_location -> to_location).
inventory_balances    per (sku, location, lot) cache, maintained in the SAME transaction
                      as the movement; only for physical / in_transit locations; never negative.
inventory_lots        FIFO cost layers. Original cost is immutable; later changes go to
                      lot_cost_adjustments (append-only).
inventory_reservations  units promised to paid orders but not yet shipped (US warehouse).
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in

LOT_SOURCES = ("purchase", "opening_balance", "return", "adjustment")

MOVEMENT_TYPES = (
    "purchase_receipt",   # SUPPLIER   -> JP_MAIN
    "opening_balance",    # ADJUSTMENT -> any physical (initial stock take)
    "transfer_ship",      # JP_MAIN    -> IN_TRANSIT
    "transfer_receive",   # IN_TRANSIT -> US_MAIN
    "transfer_loss",      # IN_TRANSIT -> LOSS
    "sale",               # US_MAIN    -> CUSTOMER
    "customer_return",    # CUSTOMER   -> US_MAIN
    "adjustment_in",      # ADJUSTMENT -> physical (stock count gain)
    "adjustment_out",     # physical   -> ADJUSTMENT (stock count loss)
    "damage",             # physical   -> LOSS
)


class MovementTypeRule(Base):
    """Allowed (from, to) location pairs per movement type, enforced by trigger.
    E.g. 'sale' is only US_MAIN -> CUSTOMER, so in-transit stock can never be sold."""

    __tablename__ = "movement_type_rules"

    movement_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    from_location_code: Mapped[str] = mapped_column(ForeignKey("locations.code"), primary_key=True)
    to_location_code: Mapped[str] = mapped_column(ForeignKey("locations.code"), primary_key=True)

    __table_args__ = (check_in("movement_type", MOVEMENT_TYPES, "type_valid"),)


class InventoryLot(IdMixin, CreatedMixin, Base):
    __tablename__ = "inventory_lots"

    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    lot_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    purchase_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_lines.id"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    original_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    original_unit_cost_base: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    base_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False, server_default="USD")

    __table_args__ = (
        Index("ix_inventory_lots_fifo", "sku_id", "received_at", "id"),
        check_in("source", LOT_SOURCES, "source_valid"),
        CheckConstraint("original_qty > 0", name="qty_positive"),
        CheckConstraint("original_unit_cost_base >= 0", name="cost_nonneg"),
        CheckConstraint("source <> 'purchase' OR purchase_line_id IS NOT NULL", name="purchase_has_line"),
    )


class LotCostAdjustment(IdMixin, CreatedMixin, Base):
    """Append-only. A late cost (e.g. freight invoice) on a lot. Applies to units still on
    hand; the portion for units already sold is booked as a cogs_adjustment financial entry."""

    __tablename__ = "lot_cost_adjustments"

    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id"), nullable=False, index=True)
    amount_total_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    qty_on_hand_at_adjustment: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_sold_at_adjustment: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        check_in("source_type", ("api", "csv", "manual", "system"), "source_type_valid"),
        CheckConstraint("qty_on_hand_at_adjustment >= 0 AND qty_sold_at_adjustment >= 0", name="qty_nonneg"),
    )


class InventoryMovement(IdMixin, CreatedMixin, Base):
    """Append-only (UPDATE/DELETE/TRUNCATE blocked by trigger)."""

    __tablename__ = "inventory_movements"

    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id"), nullable=False, index=True)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(40))
    ref_id: Mapped[int | None] = mapped_column()
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("from_location_id <> to_location_id", name="distinct_locations"),
        check_in("movement_type", MOVEMENT_TYPES, "type_valid"),
        Index("ix_inventory_movements_sku_time", "sku_id", "occurred_at"),
        Index("ix_inventory_movements_ref", "ref_type", "ref_id"),
    )


class InventoryBalance(Base):
    __tablename__ = "inventory_balances"

    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id"), primary_key=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("qty >= 0", name="qty_nonneg"),
        Index("ix_inventory_balances_loc_sku", "location_id", "sku_id"),
    )


RESERVATION_STATUSES = ("reserved", "consumed", "released")


class InventoryReservation(IdMixin, TimestampMixin, Base):
    """One row per (order line, component SKU). A bundle line reserves each component."""

    __tablename__ = "inventory_reservations"

    order_line_id: Mapped[int] = mapped_column(ForeignKey("sales_order_lines.id"), nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="reserved")
    released_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("order_line_id", "sku_id"),
        CheckConstraint("qty > 0", name="qty_positive"),
        check_in("status", RESERVATION_STATUSES, "status_valid"),
        Index("ix_inventory_reservations_open", "sku_id", "location_id", postgresql_where="status = 'reserved'"),
    )


class StockCount(IdMixin, TimestampMixin, Base):
    __tablename__ = "stock_counts"

    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    counted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (check_in("status", ("draft", "submitted", "approved", "cancelled"), "status_valid"),)


class StockCountLine(IdMixin, Base):
    __tablename__ = "stock_count_lines"

    stock_count_id: Mapped[int] = mapped_column(ForeignKey("stock_counts.id", ondelete="CASCADE"), nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    expected_qty: Mapped[int] = mapped_column(Integer, nullable=False)  # system qty; refreshed at approval
    counted_qty: Mapped[int | None] = mapped_column(Integer)
    difference_qty: Mapped[int | None] = mapped_column(Integer)  # counted - system, posted at approval

    __table_args__ = (
        UniqueConstraint("stock_count_id", "sku_id"),
        CheckConstraint("counted_qty IS NULL OR counted_qty >= 0", name="counted_nonneg"),
    )
