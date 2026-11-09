from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Currency = Literal["USD", "JPY", "TWD"]
SkuCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")]
Price = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=4)]


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: int | None = None


class CategoryOut(BaseModel):
    id: int
    name: str
    parent_id: int | None


class SkuFields(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    variant_name: str | None = Field(default=None, max_length=255)
    attributes: dict[str, str] | None = None
    barcode: str | None = Field(default=None, max_length=64)
    list_price: Price | None = None
    list_currency: Currency | None = None
    default_purchase_currency: Currency | None = None
    safety_stock_override: int | None = Field(default=None, ge=0)


class SkuCreate(SkuFields):
    sku_code: SkuCode


class SkuUpdate(SkuFields):
    model_config = ConfigDict(extra="forbid")  # e.g. sku_code / is_bundle cannot be sent
    version: int
    status: Literal["active", "discontinued"] | None = None


class ProductFields(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category_id: int | None = None
    brand: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=5000)


class ProductCreate(ProductFields):
    skus: list[SkuCreate] = Field(min_length=1, max_length=200)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    category_id: int | None = None
    brand: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "discontinued"] | None = None


class Component(BaseModel):
    sku_id: int
    qty: int = Field(ge=1, le=1000)


class BundleCreate(BaseModel):
    sku_code: SkuCode
    name: str = Field(min_length=1, max_length=255)
    category_id: int | None = None
    description: str | None = Field(default=None, max_length=5000)
    list_price: Price | None = None
    list_currency: Currency | None = None
    components: list[Component] = Field(min_length=1, max_length=50)


class ComponentsUpdate(BaseModel):
    version: int
    components: list[Component] = Field(min_length=1, max_length=50)


class SkuListItem(BaseModel):
    id: int
    sku_code: str
    name: str
    variant_name: str | None
    is_bundle: bool
    status: str
    version: int
    list_price: Decimal | None
    list_currency: str
    safety_stock_override: int | None
    product_id: int
    product_name: str
    category_id: int | None
    category_name: str | None
    jp_on_hand: int
    in_transit: int
    us_on_hand: int
    us_reserved: int
    us_available: int
    last_received_at: datetime | None
    image_url: str | None
    inventory_value_usd: Decimal | None = None


class ImageOut(BaseModel):
    id: int
    url: str
    is_primary: bool
    content_type: str
    size_bytes: int


class LotOut(BaseModel):
    id: int
    lot_no: str
    source: str
    received_at: datetime
    original_qty: int
    jp: int
    in_transit: int
    us: int
    remaining: int
    original_unit_cost_base: Decimal | None = None
    unit_cost_base: Decimal | None = None


class SkuDetail(SkuListItem):
    attributes: dict
    barcode: str | None
    default_purchase_currency: str
    brand: str | None
    description: str | None
    components: list[dict]
    used_in_bundles: list[dict]
    images: list[ImageOut]
    lots: list[LotOut]
    can_view_cost: bool


class ProductOut(BaseModel):
    id: int
    name: str
    category_id: int | None
    brand: str | None
    description: str | None
    status: str
    sku_ids: list[int]
