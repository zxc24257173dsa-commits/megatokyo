"""Replenishment settings, runs and per-SKU suggestions.

Owner inputs (2026-11-09): JP->US lead time 9 days, weekly shipments, supplier order ->
JP warehouse 9-11 days (we plan with 11, the conservative end), safety stock from
sales volatility (z * sigma_daily * sqrt(lead time)).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin
from app.models._util import check_in


class ReplenishmentSettings(Base):
    """Singleton row (id = 1)."""

    __tablename__ = "replenishment_settings"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="9")
    review_period_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="7")
    supplier_lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="11")
    ship_weekday: Mapped[int | None] = mapped_column(SmallInteger)  # 0=Mon .. 6=Sun, US Eastern
    weight_7d: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, server_default="0.5")
    weight_14d: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, server_default="0.3")
    weight_30d: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, server_default="0.2")
    safety_stock_method: Mapped[str] = mapped_column(String(20), nullable=False, server_default="volatility")
    service_level_z: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False, server_default="1.65")
    min_history_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="14")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("weight_7d + weight_14d + weight_30d = 1", name="weights_sum_1"),
        CheckConstraint("lead_time_days > 0 AND review_period_days > 0 AND supplier_lead_time_days >= 0",
                        name="days_positive"),
        CheckConstraint("ship_weekday IS NULL OR ship_weekday BETWEEN 0 AND 6", name="weekday_valid"),
        check_in("safety_stock_method", ("volatility", "days_of_cover"), "ss_method_valid"),
    )


class SkuReplenishmentParams(Base):
    __tablename__ = "sku_replenishment_params"

    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), primary_key=True)
    safety_stock_override: Mapped[int | None] = mapped_column(Integer)
    min_ship_qty: Mapped[int | None] = mapped_column(Integer)
    case_pack: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    supplier_lead_time_days: Mapped[int | None] = mapped_column(Integer)
    manual_daily_velocity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))  # for new SKUs w/o history
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (CheckConstraint("case_pack > 0", name="case_pack_positive"),)


class LivestreamSchedule(IdMixin, CreatedMixin, Base):
    __tablename__ = "livestream_schedule"

    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class ReplenishmentRun(IdMixin, CreatedMixin, Base):
    __tablename__ = "replenishment_runs"

    data_as_of: Mapped[date] = mapped_column(Date, nullable=False)  # last COMPLETE business day used
    note: Mapped[str | None] = mapped_column(Text)
    params_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="completed")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (check_in("status", ("running", "completed", "failed"), "status_valid"),)


class ReplenishmentSuggestion(IdMixin, Base):
    __tablename__ = "replenishment_suggestions"

    run_id: Mapped[int] = mapped_column(ForeignKey("replenishment_runs.id", ondelete="CASCADE"), nullable=False)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False)
    v7: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    v14: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    v30: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    v_blended: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    sigma_daily: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    livestream_factor: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, server_default="1")
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_demand: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    safety_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    committed_demand: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    us_available: Mapped[int] = mapped_column(Integer, nullable=False)
    in_transit_in_time: Mapped[int] = mapped_column(Integer, nullable=False)
    in_transit_late: Mapped[int] = mapped_column(Integer, nullable=False)
    jp_available: Mapped[int] = mapped_column(Integer, nullable=False)
    days_to_stockout: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    stockout_date: Mapped[date | None] = mapped_column(Date)
    latest_ship_date: Mapped[date | None] = mapped_column(Date)
    suggested_ship_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    suggested_purchase_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status_color: Mapped[str] = mapped_column(String(10), nullable=False)
    explanation: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Phase 10 (direct ship JP -> US): what to do for this SKU and why
    action: Mapped[str] = mapped_column(String(20), nullable=False, server_default="none")  # buy / open_box / none
    on_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")  # purchased, not shipped yet
    boxes_to_open: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    source_box_sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"))
    latest_order_date: Mapped[date | None] = mapped_column(Date)
    override_ship_qty: Mapped[int | None] = mapped_column(Integer)
    override_purchase_qty: Mapped[int | None] = mapped_column(Integer)
    override_reason: Mapped[str | None] = mapped_column(Text)
    overridden_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("run_id", "sku_id"),
        check_in("status_color", ("red", "orange", "yellow", "green"), "color_valid"),
        check_in("action", ("buy", "open_box", "none"), "action_valid"),
        CheckConstraint("suggested_ship_qty >= 0 AND suggested_purchase_qty >= 0", name="suggest_nonneg"),
        CheckConstraint(
            "(override_ship_qty IS NULL AND override_purchase_qty IS NULL) OR override_reason IS NOT NULL",
            name="override_needs_reason",
        ),
    )
