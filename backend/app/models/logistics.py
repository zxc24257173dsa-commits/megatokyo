"""Japan -> US shipments.

shipped : JP_MAIN    -> IN_TRANSIT  (FIFO lots)
received: IN_TRANSIT -> US_MAIN     (actual received qty; partial receipts allowed)
short   : IN_TRANSIT -> LOSS        (with reason, approved)
"""
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in

SHIPMENT_STATUSES = ("draft", "packed", "shipped", "delivered", "received", "closed", "cancelled")


class Carrier(IdMixin, CreatedMixin, Base):
    __tablename__ = "carriers"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    tracking_url_template: Mapped[str | None] = mapped_column(Text)


class Shipment(IdMixin, TimestampMixin, Base):
    __tablename__ = "shipments"

    shipment_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    from_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    to_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), nullable=False)
    carrier_id: Mapped[int | None] = mapped_column(ForeignKey("carriers.id"))
    tracking_no: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    planned_ship_date: Mapped[date | None] = mapped_column(Date)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eta: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replenishment_run_id: Mapped[int | None] = mapped_column(ForeignKey("replenishment_runs.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        check_in("status", SHIPMENT_STATUSES, "status_valid"),
        CheckConstraint("from_location_id <> to_location_id", name="distinct_locations"),
        CheckConstraint("status NOT IN ('shipped','delivered','received','closed') OR shipped_at IS NOT NULL",
                        name="shipped_has_date"),
        Index("ix_shipments_status_eta", "status", "eta"),
    )


class ShipmentLine(IdMixin, Base):
    __tablename__ = "shipment_lines"

    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    qty_planned: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qty_shipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qty_received: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qty_lost: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    jp_on_hand_snapshot: Mapped[int | None] = mapped_column(Integer)
    discrepancy_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("shipment_id", "sku_id"),
        CheckConstraint("qty_planned >= 0 AND qty_shipped >= 0 AND qty_received >= 0 AND qty_lost >= 0",
                        name="qty_nonneg"),
        CheckConstraint("qty_received + qty_lost <= qty_shipped", name="received_le_shipped"),
    )


class ShipmentEvent(IdMixin, Base):
    """Append-only."""

    __tablename__ = "shipment_events"

    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
