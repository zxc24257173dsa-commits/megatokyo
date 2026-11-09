"""Products, SKUs (variants), bundles and images. All writes are audited.

Rules:
  * SKU codes are unique (case-insensitive) and immutable once created - orders, cost sheets
    and Whatnot mappings refer to them.
  * Nothing is deleted: a SKU is set to 'discontinued'.
  * Edits carry the version the user loaded; if someone else saved first -> 409.
  * A bundle is a SKU with is_bundle=true and >=1 component SKU (components cannot be bundles).
"""
from decimal import Decimal

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.catalog import Category, Product, Sku, SkuImage, sku_bundle_components
from app.services import audit, storage
from app.services.audit import RequestContext

SKU_FIELDS = ("name", "variant_name", "attributes", "barcode", "list_price", "list_currency",
              "default_purchase_currency", "safety_stock_override", "status")
PRODUCT_FIELDS = ("name", "category_id", "brand", "description", "status")


def _jsonable(v):
    return str(v) if isinstance(v, Decimal) else v


def sku_snapshot(db: Session, sku: Sku) -> dict:
    snap = {f: _jsonable(getattr(sku, f)) for f in ("sku_code", "is_bundle", *SKU_FIELDS)}
    if sku.is_bundle:
        snap["components"] = bundle_components(db, sku.id)
    return snap


def bundle_components(db: Session, bundle_sku_id: int) -> list[dict]:
    rows = db.execute(
        select(sku_bundle_components.c.component_sku_id, sku_bundle_components.c.qty, Sku.sku_code)
        .join(Sku, Sku.id == sku_bundle_components.c.component_sku_id)
        .where(sku_bundle_components.c.bundle_sku_id == bundle_sku_id)
        .order_by(Sku.sku_code)
    ).all()
    return [{"sku_id": r[0], "qty": r[1], "sku_code": r[2]} for r in rows]


# ---------- categories ----------

def create_category(db: Session, *, actor_id: int, name: str, parent_id: int | None, ctx: RequestContext) -> Category:
    name = name.strip()
    if db.scalar(select(Category.id).where(func.lower(Category.name) == name.lower())):
        raise AppError("category_exists", "A category with this name already exists", 409)
    if parent_id is not None and db.get(Category, parent_id) is None:
        raise AppError("not_found", "Parent category not found", 404)
    cat = Category(name=name, parent_id=parent_id)
    db.add(cat)
    db.flush()
    audit.record(db, action="category.created", entity_type="category", entity_id=cat.id, actor_id=actor_id,
                 after={"name": name, "parent_id": parent_id}, ctx=ctx)
    return cat


# ---------- products & SKUs ----------

def _check_codes_free(db: Session, codes: list[str]) -> None:
    norm = [c.strip().upper() for c in codes]
    dupes = sorted({c for c in norm if norm.count(c) > 1})
    if dupes:
        raise AppError("duplicate_sku_code", "The same SKU code appears more than once", 422, {"sku_codes": dupes})
    taken = sorted(db.scalars(select(Sku.sku_code).where(func.upper(Sku.sku_code).in_(norm))))
    if taken:
        raise AppError("sku_code_taken", "SKU code already exists", 409, {"sku_codes": taken})


def _check_category(db: Session, category_id: int | None) -> None:
    if category_id is not None and db.get(Category, category_id) is None:
        raise AppError("not_found", "Category not found", 404)


def _new_sku(product_id: int, data: dict, is_bundle: bool = False) -> Sku:
    code = data["sku_code"].strip()
    if not code:
        raise AppError("invalid_sku_code", "SKU code is required", 422)
    return Sku(product_id=product_id, sku_code=code, is_bundle=is_bundle,
               **{k: v for k, v in data.items() if k in SKU_FIELDS and v is not None})


def create_product(db: Session, *, actor_id: int, product: dict, skus: list[dict], ctx: RequestContext) -> Product:
    if not skus:
        raise AppError("skus_required", "A product needs at least one SKU", 422)
    _check_category(db, product.get("category_id"))
    _check_codes_free(db, [s["sku_code"] for s in skus])
    p = Product(**{k: v for k, v in product.items() if k in PRODUCT_FIELDS and v is not None})
    db.add(p)
    db.flush()
    for data in skus:
        sku = _new_sku(p.id, {"name": data.get("name") or p.name, **{k: v for k, v in data.items() if k != "name"}})
        db.add(sku)
        db.flush()
        audit.record(db, action="sku.created", entity_type="sku", entity_id=sku.id, actor_id=actor_id,
                     after=sku_snapshot(db, sku), ctx=ctx)
    audit.record(db, action="product.created", entity_type="product", entity_id=p.id, actor_id=actor_id,
                 after={f: getattr(p, f) for f in PRODUCT_FIELDS}, ctx=ctx)
    return p


def update_product(db: Session, *, actor_id: int, product_id: int, changes: dict, ctx: RequestContext) -> Product:
    p = db.get(Product, product_id, with_for_update=True, populate_existing=True)
    if p is None:
        raise AppError("not_found", "Product not found", 404)
    if "category_id" in changes:
        _check_category(db, changes["category_id"])
    before = {f: getattr(p, f) for f in PRODUCT_FIELDS}
    for k, v in changes.items():
        if k in PRODUCT_FIELDS:
            setattr(p, k, v)
    db.flush()
    after = {f: getattr(p, f) for f in PRODUCT_FIELDS}
    if after != before:
        audit.record(db, action="product.updated", entity_type="product", entity_id=p.id, actor_id=actor_id,
                     before=before, after=after, ctx=ctx)
    return p


def add_sku(db: Session, *, actor_id: int, product_id: int, data: dict, ctx: RequestContext) -> Sku:
    p = db.get(Product, product_id)
    if p is None:
        raise AppError("not_found", "Product not found", 404)
    _check_codes_free(db, [data["sku_code"]])
    sku = _new_sku(p.id, {"name": data.get("name") or p.name, **{k: v for k, v in data.items() if k != "name"}})
    db.add(sku)
    db.flush()
    audit.record(db, action="sku.created", entity_type="sku", entity_id=sku.id, actor_id=actor_id,
                 after=sku_snapshot(db, sku), ctx=ctx)
    return sku


def get_sku(db: Session, sku_id: int, *, for_update: bool = False) -> Sku:
    sku = db.get(Sku, sku_id, with_for_update=for_update, populate_existing=for_update)
    if sku is None:
        raise AppError("not_found", "SKU not found", 404)
    return sku


def _check_version(sku: Sku, version: int) -> None:
    if sku.version != version:
        raise AppError("modified_by_someone_else",
                       "This SKU was changed by another user; reload and retry", 409,
                       {"current_version": sku.version})


def update_sku(db: Session, *, actor_id: int, sku_id: int, version: int, changes: dict,
               ctx: RequestContext) -> Sku:
    if "sku_code" in changes or "is_bundle" in changes:
        raise AppError("immutable_field", "SKU code and bundle type cannot be changed", 422)
    sku = get_sku(db, sku_id, for_update=True)
    _check_version(sku, version)
    before = sku_snapshot(db, sku)
    for k, v in changes.items():
        if k in SKU_FIELDS:
            setattr(sku, k, v)
    after_candidate = {**before, **{k: _jsonable(v) for k, v in changes.items() if k in SKU_FIELDS}}
    if after_candidate != before:
        db.flush()  # bumps version
        audit.record(db, action="sku.updated", entity_type="sku", entity_id=sku.id, actor_id=actor_id,
                     before=before, after=sku_snapshot(db, sku), ctx=ctx)
    return sku


# ---------- bundles ----------

def _validate_components(db: Session, components: list[dict]) -> None:
    if not components:
        raise AppError("components_required", "A bundle needs at least one component SKU", 422)
    ids = [c["sku_id"] for c in components]
    if len(ids) != len(set(ids)):
        raise AppError("duplicate_component", "Each component SKU may appear only once", 422)
    rows = {s.id: s for s in db.scalars(select(Sku).where(Sku.id.in_(ids)))}
    missing = [i for i in ids if i not in rows]
    if missing:
        raise AppError("not_found", "Component SKU not found", 404, {"sku_ids": missing})
    nested = [rows[i].sku_code for i in ids if rows[i].is_bundle]
    if nested:
        raise AppError("nested_bundle", "A bundle cannot contain another bundle", 422, {"sku_codes": nested})
    if any(c["qty"] <= 0 for c in components):
        raise AppError("invalid_qty", "Component quantity must be at least 1", 422)


def create_bundle(db: Session, *, actor_id: int, data: dict, components: list[dict], ctx: RequestContext) -> Sku:
    _check_category(db, data.get("category_id"))
    _check_codes_free(db, [data["sku_code"]])
    _validate_components(db, components)
    p = Product(name=data["name"], category_id=data.get("category_id"), description=data.get("description"))
    db.add(p)
    db.flush()
    sku = _new_sku(p.id, {k: v for k, v in data.items() if k not in ("category_id", "description")}, is_bundle=True)
    db.add(sku)
    db.flush()
    db.execute(insert(sku_bundle_components), [
        {"bundle_sku_id": sku.id, "component_sku_id": c["sku_id"], "qty": c["qty"]} for c in components
    ])
    audit.record(db, action="bundle.created", entity_type="sku", entity_id=sku.id, actor_id=actor_id,
                 after=sku_snapshot(db, sku), ctx=ctx)
    return sku


def set_bundle_components(db: Session, *, actor_id: int, sku_id: int, version: int, components: list[dict],
                          ctx: RequestContext) -> Sku:
    sku = get_sku(db, sku_id, for_update=True)
    if not sku.is_bundle:
        raise AppError("not_a_bundle", "This SKU is not a bundle", 422)
    _check_version(sku, version)
    _validate_components(db, components)
    before = sku_snapshot(db, sku)
    db.execute(delete(sku_bundle_components).where(sku_bundle_components.c.bundle_sku_id == sku.id))
    db.execute(insert(sku_bundle_components), [
        {"bundle_sku_id": sku.id, "component_sku_id": c["sku_id"], "qty": c["qty"]} for c in components
    ])
    # Bump the version so concurrent editors notice the composition changed.
    db.execute(update(Sku).where(Sku.id == sku.id).values(version=Sku.version + 1))
    db.refresh(sku)
    audit.record(db, action="bundle.components_changed", entity_type="sku", entity_id=sku.id, actor_id=actor_id,
                 before=before, after=sku_snapshot(db, sku), ctx=ctx)
    return sku


# ---------- images ----------

def add_image(db: Session, *, actor_id: int, sku_id: int, data: bytes, ctx: RequestContext) -> SkuImage:
    from app.core.config import get_settings
    get_sku(db, sku_id)
    if not data:
        raise AppError("empty_file", "The file is empty", 422)
    if len(data) > get_settings().max_image_bytes:
        raise AppError("file_too_large", "Image must be 5 MB or smaller", 413)
    content_type, ext = storage.sniff_image(data)
    key = storage.save(f"sku-images/{sku_id}", data, ext)
    has_primary = db.scalar(select(SkuImage.id).where(SkuImage.sku_id == sku_id, SkuImage.is_primary))
    next_order = (db.scalar(select(func.max(SkuImage.sort_order)).where(SkuImage.sku_id == sku_id)) or 0) + 1
    img = SkuImage(sku_id=sku_id, storage_key=key, content_type=content_type, size_bytes=len(data),
                   sort_order=next_order, is_primary=has_primary is None, created_by=actor_id)
    db.add(img)
    db.flush()
    audit.record(db, action="sku.image_added", entity_type="sku", entity_id=sku_id, actor_id=actor_id,
                 after={"image_id": img.id, "content_type": content_type, "size_bytes": len(data)}, ctx=ctx)
    return img


def _get_image(db: Session, sku_id: int, image_id: int) -> SkuImage:
    img = db.get(SkuImage, image_id)
    if img is None or img.sku_id != sku_id:
        raise AppError("not_found", "Image not found", 404)
    return img


def set_primary_image(db: Session, *, actor_id: int, sku_id: int, image_id: int, ctx: RequestContext) -> None:
    img = _get_image(db, sku_id, image_id)
    db.execute(update(SkuImage).where(SkuImage.sku_id == sku_id, SkuImage.is_primary).values(is_primary=False))
    db.flush()
    img.is_primary = True
    audit.record(db, action="sku.image_primary", entity_type="sku", entity_id=sku_id, actor_id=actor_id,
                 after={"image_id": image_id}, ctx=ctx)


def delete_image(db: Session, *, actor_id: int, sku_id: int, image_id: int, ctx: RequestContext) -> str:
    """Returns the storage key; the caller deletes the file only AFTER the transaction commits."""
    img = _get_image(db, sku_id, image_id)
    was_primary, key = img.is_primary, img.storage_key
    db.delete(img)
    db.flush()
    if was_primary:
        nxt = db.scalar(select(SkuImage).where(SkuImage.sku_id == sku_id).order_by(SkuImage.sort_order).limit(1))
        if nxt:
            nxt.is_primary = True
    audit.record(db, action="sku.image_deleted", entity_type="sku", entity_id=sku_id, actor_id=actor_id,
                 before={"image_id": image_id}, ctx=ctx)
    return key
