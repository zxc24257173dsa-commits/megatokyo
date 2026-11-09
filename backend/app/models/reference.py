"""Currencies, FX rates, locations (warehouses / virtual ledger locations)."""
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin
from app.models._util import check_in


class Currency(Base):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    minor_units: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class FxRate(IdMixin, CreatedMixin, Base):
    """1 base_ccy = rate quote_ccy on rate_date, from a named source."""

    __tablename__ = "fx_rates"

    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_ccy: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    quote_ccy: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(60), nullable=False)

    __table_args__ = (
        UniqueConstraint("rate_date", "base_ccy", "quote_ccy", "source"),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint("base_ccy <> quote_ccy", name="distinct_ccy"),
    )


LOCATION_KINDS = ("physical", "in_transit", "virtual")


class Location(IdMixin, CreatedMixin, Base):
    """physical/in_transit locations hold stock (balances kept, never negative);
    virtual locations (SUPPLIER, CUSTOMER, ADJUSTMENT, LOSS) are ledger endpoints only."""

    __tablename__ = "locations"

    code: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    timezone: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (check_in("kind", LOCATION_KINDS, "kind_valid"),)
