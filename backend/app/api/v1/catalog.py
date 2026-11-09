"""/api/v1 catalog: categories, products, SKUs, bundles, images, signed file downloads."""
from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_request_context, require_permissions
from app.core.errors import AppError
from app.db.session import get_db
from app.models.catalog import Category, Product, Sku, SkuImage, sku_bundle_components
from app.schemas.catalog import (
    BundleCreate, CategoryIn, CategoryOut, ComponentsUpdate, ImageOut, LotOut, ProductCreate, ProductOut,
    ProductUpdate, SkuCreate, SkuDetail, SkuListItem, SkuUpdate,
)
from app.services import catalog as svc
from app.services import inventory_queries as q
from app.services import storage
from app.services.audit import RequestContext

router = APIRouter(tags=["catalog"])
view = require_permissions("catalog.view")
edit = require_permissions("catalog.edit")


# ---------- categories ----------

@router.get("/categories", response_model=list[CategoryOut])
def list_categories(_: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return [CategoryOut(id=c.id, name=c.name, parent_id=c.parent_id)
            for c in db.scalars(select(Category).order_by(Category.name))]


@router.post("/categories", response_model=CategoryOut, status_code=201)
def create_category(body: CategoryIn, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
                    ctx: RequestContext = Depends(get_request_context)):
    c = svc.create_category(db, actor_id=current.id, name=body.name, parent_id=body.parent_id, ctx=ctx)
    db.commit()
    return CategoryOut(id=c.id, name=c.name, parent_id=c.parent_id)


# ---------- products ----------

def _product_out(db: Session, p: Product) -> ProductOut:
    ids = list(db.scalars(select(Sku.id).where(Sku.product_id == p.id).order_by(Sku.sku_code)))
    return ProductOut(id=p.id, name=p.name, category_id=p.category_id, brand=p.brand, description=p.description,
                      status=p.status, sku_ids=ids)


@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: ProductCreate, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
                   ctx: RequestContext = Depends(get_request_context)):
    p = svc.create_product(db, actor_id=current.id, product=body.model_dump(exclude={"skus"}),
                           skus=[s.model_dump(exclude_none=True) for s in body.skus], ctx=ctx)
    db.commit()
    return _product_out(db, p)


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, body: ProductUpdate, current: CurrentUser = Depends(edit),
                   db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    p = svc.update_product(db, actor_id=current.id, product_id=product_id,
                           changes=body.model_dump(exclude_unset=True), ctx=ctx)
    db.commit()
    return _product_out(db, p)


@router.post("/products/{product_id}/skus", response_model=SkuDetail, status_code=201)
def add_sku(product_id: int, body: SkuCreate, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
            ctx: RequestContext = Depends(get_request_context)):
    sku = svc.add_sku(db, actor_id=current.id, product_id=product_id, data=body.model_dump(exclude_none=True), ctx=ctx)
    db.commit()
    return _detail(db, sku.id, current)


# ---------- SKUs ----------

@router.get("/skus", response_model=list[SkuListItem])
def list_skus(search: str | None = Query(default=None, max_length=100), category_id: int | None = None,
              status: str | None = Query(default="active", pattern="^(active|discontinued)$"),
              include_bundles: bool = True, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return q.sku_rows(db, search=search, category_id=category_id, status=status, include_bundles=include_bundles,
                      with_cost=q.can_view_cost(current.permissions))


def _detail(db: Session, sku_id: int, current: CurrentUser) -> SkuDetail:
    with_cost = q.can_view_cost(current.permissions)
    sku = svc.get_sku(db, sku_id)
    row = q.sku_rows(db, status=None, with_cost=with_cost, sku_id=sku_id)[0]
    product = db.get(Product, sku.product_id)
    used_in = db.execute(
        select(Sku.id, Sku.sku_code, Sku.name, sku_bundle_components.c.qty)
        .join(sku_bundle_components, sku_bundle_components.c.bundle_sku_id == Sku.id)
        .where(sku_bundle_components.c.component_sku_id == sku_id)
    ).all()
    images = db.scalars(select(SkuImage).where(SkuImage.sku_id == sku_id).order_by(SkuImage.sort_order))
    return SkuDetail(
        **row, attributes=sku.attributes, barcode=sku.barcode, default_purchase_currency=sku.default_purchase_currency,
        brand=product.brand, description=product.description,
        components=svc.bundle_components(db, sku_id) if sku.is_bundle else [],
        used_in_bundles=[{"sku_id": u[0], "sku_code": u[1], "name": u[2], "qty": u[3]} for u in used_in],
        images=[ImageOut(id=i.id, url=storage.signed_url(i.storage_key), is_primary=i.is_primary,
                         content_type=i.content_type, size_bytes=i.size_bytes) for i in images],
        lots=[] if sku.is_bundle else [LotOut(**l) for l in q.lots(db, sku_id, with_cost=with_cost)],
        can_view_cost=with_cost,
    )


@router.get("/skus/{sku_id}", response_model=SkuDetail)
def get_sku(sku_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return _detail(db, sku_id, current)


@router.patch("/skus/{sku_id}", response_model=SkuDetail)
def update_sku(sku_id: int, body: SkuUpdate, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
               ctx: RequestContext = Depends(get_request_context)):
    svc.update_sku(db, actor_id=current.id, sku_id=sku_id, version=body.version,
                   changes=body.model_dump(exclude_unset=True, exclude={"version"}), ctx=ctx)
    db.commit()
    return _detail(db, sku_id, current)


# ---------- bundles ----------

@router.post("/bundles", response_model=SkuDetail, status_code=201)
def create_bundle(body: BundleCreate, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
                  ctx: RequestContext = Depends(get_request_context)):
    sku = svc.create_bundle(db, actor_id=current.id, data=body.model_dump(exclude={"components"}, exclude_none=True),
                            components=[c.model_dump() for c in body.components], ctx=ctx)
    db.commit()
    return _detail(db, sku.id, current)


@router.put("/skus/{sku_id}/components", response_model=SkuDetail)
def set_components(sku_id: int, body: ComponentsUpdate, current: CurrentUser = Depends(edit),
                   db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    svc.set_bundle_components(db, actor_id=current.id, sku_id=sku_id, version=body.version,
                              components=[c.model_dump() for c in body.components], ctx=ctx)
    db.commit()
    return _detail(db, sku_id, current)


# ---------- images ----------

@router.post("/skus/{sku_id}/images", response_model=ImageOut, status_code=201)
async def upload_image(sku_id: int, file: UploadFile = File(...), current: CurrentUser = Depends(edit),
                       db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    from app.core.config import get_settings
    data = await file.read(get_settings().max_image_bytes + 1)
    img = svc.add_image(db, actor_id=current.id, sku_id=sku_id, data=data, ctx=ctx)
    db.commit()
    return ImageOut(id=img.id, url=storage.signed_url(img.storage_key), is_primary=img.is_primary,
                    content_type=img.content_type, size_bytes=img.size_bytes)


@router.post("/skus/{sku_id}/images/{image_id}/primary", status_code=204)
def set_primary(sku_id: int, image_id: int, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
                ctx: RequestContext = Depends(get_request_context)):
    svc.set_primary_image(db, actor_id=current.id, sku_id=sku_id, image_id=image_id, ctx=ctx)
    db.commit()
    return Response(status_code=204)


@router.delete("/skus/{sku_id}/images/{image_id}", status_code=204)
def delete_image(sku_id: int, image_id: int, current: CurrentUser = Depends(edit), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    key = svc.delete_image(db, actor_id=current.id, sku_id=sku_id, image_id=image_id, ctx=ctx)
    db.commit()
    storage.delete(key)  # only after the database change is committed
    return Response(status_code=204)


@router.get("/files/{key:path}", include_in_schema=False)
def get_file(key: str, exp: int, sig: str):
    """Signed, expiring URL (no login header needed, so <img src> works)."""
    if not storage.verify_signature(key, exp, sig):
        raise AppError("not_found", "File not found or link expired", 404)
    path = storage.path_for(key)
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(path.suffix.lstrip("."),
                                                                             "application/octet-stream")
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})
