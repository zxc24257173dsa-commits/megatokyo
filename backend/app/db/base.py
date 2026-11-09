"""Declarative base, shared column types and naming convention.

Money:       NUMERIC(18,4)   - never float
Unit cost:   NUMERIC(18,6)   - extra precision to avoid rounding drift in FIFO
FX rate:     NUMERIC(18,8)
Quantities:  INTEGER (units)
Timestamps:  TIMESTAMPTZ, stored in UTC
"""
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, Identity, MetaData, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Annotated[Decimal, mapped_column(Numeric(18, 4))]
UnitCost = Annotated[Decimal, mapped_column(Numeric(18, 6))]
FxRate = Annotated[Decimal, mapped_column(Numeric(18, 8))]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Ids and FKs are BIGINT; quantities declare Integer explicitly.
    type_annotation_map = {int: BigInteger}


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)


class CreatedMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TimestampMixin(CreatedMixin):
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
