"""Products, SKUs, bundles (one sellable item made of several SKUs), suppliers,
and mappings from external platform ids (Whatnot) to our SKUs."""
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, Numeric, String, Table, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.models._util import check_in


class Category(IdMixin, CreatedMixin, Base):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))


class Product(IdMixin, TimestampMixin, Base):
    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), index=True)
    brand: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")

    __table_args__ = (check_in("status", ("active", "discontinued"), "status_valid"),)


class Sku(IdMixin, TimestampMixin, Base):
    """A stock-keeping unit. Bundles (is_bundle=true) hold no stock themselves:
    selling one consumes its components (see sku_bundle_components)."""

    __tablename__ = "skus"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False, index=True)
    sku_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    variant_name: Mapped[str | None] = mapped_column(String(255))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    barcode: Mapped[str | None] = mapped_column(String(64))
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    list_currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False, server_default="USD")
    default_purchase_currency: Mapped[str] = mapped_column(
        ForeignKey("currencies.code"), nullable=False, server_default="JPY"
    )
    is_bundle: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    safety_stock_override: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        # SKU codes are unique case-insensitively: "ab-1" and "AB-1" cannot coexist.
        Index("uq_skus_sku_code_upper", func.upper(sku_code), unique=True),
        CheckConstraint("sku_code = btrim(sku_code) AND sku_code <> ''", name="sku_code_trimmed"),
        CheckConstraint("list_price IS NULL OR list_price >= 0", name="list_price_nonneg"),
        CheckConstraint("safety_stock_override IS NULL OR safety_stock_override >= 0", name="safety_nonneg"),
        check_in("status", ("active", "discontinued"), "status_valid"),
    )


class SkuImage(IdMixin, CreatedMixin, Base):
    __tablename__ = "sku_images"

    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


sku_bundle_components = Table(
    "sku_bundle_components",
    Base.metadata,
    Column("bundle_sku_id", BigInteger, ForeignKey("skus.id"), primary_key=True),
    Column("component_sku_id", BigInteger, ForeignKey("skus.id"), primary_key=True),
    Column("qty", Integer, nullable=False),
    CheckConstraint("qty > 0", name="qty_positive"),
    CheckConstraint("bundle_sku_id <> component_sku_id", name="not_self"),
)


class ExternalSkuMapping(IdMixin, CreatedMixin, Base):
    """Maps a platform product/variant (e.g. a Whatnot ProductVariant) to one of our SKUs
    (which may be a bundle)."""

    __tablename__ = "external_sku_mappings"

    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    external_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_variant_id: Mapped[str | None] = mapped_column(String(128))
    external_sku: Mapped[str | None] = mapped_column(String(128))
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), nullable=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        UniqueConstraint(
            "platform", "external_product_id", "external_variant_id",
            name="uq_external_sku_mappings_ref", postgresql_nulls_not_distinct=True,
        ),
    )


class Supplier(IdMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    country: Mapped[str | None] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(ForeignKey("currencies.code"), nullable=False, server_default="JPY")
    default_lead_time_days: Mapped[int | None] = mapped_column(Integer)
    contact: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    note: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        CheckConstraint("default_lead_time_days IS NULL OR default_lead_time_days >= 0", name="lead_time_nonneg"),
    )
