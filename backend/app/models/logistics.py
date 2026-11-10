"""Inbound parcels Japan -> US (Phase 9).

Goods are bought in Japan and sent straight to the US warehouse (owner decision), so a parcel is
a set of purchase lines shipped together under one tracking number:

  shipped : SUPPLIER   -> IN_TRANSIT  (new lot per purchase line, linked to the parcel line)
  received: IN_TRANSIT -> US_MAIN     (actual qty; the rest can stay in transit for a later receipt)
  lost    : IN_TRANSIT -> LOSS        (with reason; booked as an inventory loss in the P&L)
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
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    packages: Mapped[int | None] = mapped_column(Integer)  # number of boxes in this parcel / shipment

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
    purchase_line_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_lines.id"), index=True)
    qty_planned: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qty_shipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qty_received: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    qty_lost: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    jp_on_hand_snapshot: Mapped[int | None] = mapped_column(Integer)
    discrepancy_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("shipment_id", "purchase_line_id"),
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
