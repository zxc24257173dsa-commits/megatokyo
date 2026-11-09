"""Rebuildable aggregates for fast trend/dashboard queries. The ledgers stay the source of truth."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DailySkuSales(Base):
    __tablename__ = "daily_sku_sales"

    biz_date: Mapped[date] = mapped_column(Date, primary_key=True)  # America/New_York
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), primary_key=True)
    qty_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    livestream_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    revenue_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    cogs_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    refund_base: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
