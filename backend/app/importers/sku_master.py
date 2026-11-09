"""Product / SKU master sheet: create new SKUs and update existing ones (name, category,
variant, selling price, safety stock, status, Whatnot listing id).

Inventory columns in the sheet (Japan / US / in-transit / reserved / available) and the
"standard cost" column are intentionally ignored: stock only comes from real movements and
cost only from purchase batches.
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.importers import normalize as n
from app.importers.purchase_cost import EXAMPLE_RE, RowResult
from app.models.catalog import Category, ExternalSkuMapping, Product, Sku
from app.services import audit
from app.services.audit import RequestContext

STATUS_ALIASES = {"active": "active", "有效": "active", "販售中": "active", "販売中": "active", "on sale": "active",
                  "discontinued": "discontinued", "inactive": "discontinued", "停售": "discontinued",
                  "已停售": "discontinued", "販売終了": "discontinued"}


def validate(db: Session, rows: list[tuple[int, dict, dict]], job_id: int) -> list[RowResult]:
    results = []
    for row_number, cells, raw in rows:
        r = RowResult(row_number, raw)
        results.append(r)
        g = cells.get
        if any(EXAMPLE_RE.search(n.text(g(k)) or "") for k in ("product_name", "notes")):
            r.action = "skip_example"
            r.warnings.append("範例列，不會匯入")
            continue
        nz = r.normalized
        for k in ("sku_code", "product_name", "category", "variant", "whatnot_listing_id", "notes"):
            nz[k] = n.text(g(k))
        if not nz["sku_code"]:
            r.errors.append("缺少 SKU")
        if not nz["product_name"]:
            r.errors.append("缺少商品名稱")
        price, err = n.decimal(g("list_price"))
        if err:
            r.errors.append(f"售價：{err}")
        elif price is not None:
            if price < 0:
                r.errors.append("售價不能是負數")
            nz["list_price"] = str(price)
        cur, err = n.currency(g("list_currency"))
        if err:
            r.errors.append(err)
        elif cur:
            nz["list_currency"] = cur
        ss, err = n.integer(g("safety_stock"), allow_blank=True)
        if err:
            r.errors.append(f"安全庫存：{err}")
        elif ss is not None:
            if ss < 0:
                r.errors.append("安全庫存不能是負數")
            nz["safety_stock"] = ss
        st = n.text(g("status"))
        if st:
            mapped = STATUS_ALIASES.get(st.lower()) or STATUS_ALIASES.get(st)
            if mapped is None:
                r.errors.append(f"無法辨識的狀態：{st}（Active / Discontinued）")
            nz["status"] = mapped

    active = [r for r in results if r.action != "skip_example"]
    codes = [r.normalized["sku_code"].upper() for r in active if r.normalized.get("sku_code")]
    dupes = {c for c in codes if codes.count(c) > 1}
    existing = {s.sku_code.upper(): s for s in db.scalars(select(Sku).where(func.upper(Sku.sku_code).in_(codes)))}
    listing_owner = {m.external_product_id: m.sku_id for m in db.scalars(
        select(ExternalSkuMapping).where(ExternalSkuMapping.platform == "whatnot"))}
    for r in active:
        code = (r.normalized.get("sku_code") or "").upper()
        if code in dupes:
            r.errors.append("同一個 SKU 在檔案中出現多次")
        sku = existing.get(code)
        r.matched_sku_id = sku.id if sku else None
        lid = r.normalized.get("whatnot_listing_id")
        if lid and lid in listing_owner and listing_owner[lid] != r.matched_sku_id:
            r.errors.append(f"Whatnot Listing ID {lid} 已對應到其他 SKU")
        if r.errors:
            r.action = "error"
        elif sku is None:
            r.action = "create"
        else:
            r.action = "update" if _changes(db, sku, r.normalized) else "unchanged"
            if r.action == "update":
                r.warnings.append("將更新：" + "、".join(_changes(db, sku, r.normalized)))
    return results


def _changes(db: Session, sku: Sku, nz: dict) -> dict:
    product = db.get(Product, sku.product_id)
    changes = {}
    if nz.get("product_name") and nz["product_name"] != sku.name:
        changes["名稱"] = nz["product_name"]
    if nz.get("variant") is not None and nz["variant"] != sku.variant_name:
        changes["規格"] = nz["variant"]
    if nz.get("list_price") is not None and (sku.list_price is None or Decimal(nz["list_price"]) != sku.list_price):
        changes["售價"] = nz["list_price"]
    if nz.get("list_currency") and nz["list_currency"] != sku.list_currency:
        changes["售價幣別"] = nz["list_currency"]
    if nz.get("safety_stock") is not None and nz["safety_stock"] != sku.safety_stock_override:
        changes["安全庫存"] = nz["safety_stock"]
    if nz.get("status") and nz["status"] != sku.status:
        changes["狀態"] = nz["status"]
    if nz.get("category"):
        cat = db.get(Category, product.category_id) if product.category_id else None
        if cat is None or cat.name.lower() != nz["category"].lower():
            changes["分類"] = nz["category"]
    if nz.get("whatnot_listing_id") and not db.scalar(select(ExternalSkuMapping.id).where(
            ExternalSkuMapping.platform == "whatnot", ExternalSkuMapping.external_product_id == nz["whatnot_listing_id"])):
        changes["Whatnot Listing"] = nz["whatnot_listing_id"]
    return changes


def commit(db: Session, *, actor_id: int, job_id: int, results: list[RowResult], create_missing_skus: bool,
           ctx: RequestContext) -> dict:
    todo = [r for r in results if r.action in ("create", "update")]
    if not todo:
        raise AppError("nothing_to_import", "There are no new or changed SKUs to import", 422)
    cats: dict[str, int] = {}

    def category_id(name: str | None) -> int | None:
        if not name:
            return None
        key = name.lower()
        if key not in cats:
            cid = db.scalar(select(Category.id).where(func.lower(Category.name) == key))
            if cid is None:
                c = Category(name=name)
                db.add(c)
                db.flush()
                cid = c.id
            cats[key] = cid
        return cats[key]

    created = updated = 0
    for r in todo:
        nz = r.normalized
        if r.action == "create":
            p = Product(name=nz["product_name"], category_id=category_id(nz.get("category")))
            db.add(p)
            db.flush()
            sku = Sku(product_id=p.id, sku_code=nz["sku_code"], name=nz["product_name"], variant_name=nz.get("variant"),
                      list_price=Decimal(nz["list_price"]) if nz.get("list_price") else None,
                      list_currency=nz.get("list_currency") or "USD", safety_stock_override=nz.get("safety_stock"),
                      status=nz.get("status") or "active")
            db.add(sku)
            db.flush()
            created += 1
            before = None
        else:
            sku = db.get(Sku, r.matched_sku_id, with_for_update=True, populate_existing=True)
            before = {"name": sku.name, "variant_name": sku.variant_name, "list_price": str(sku.list_price),
                      "safety_stock_override": sku.safety_stock_override, "status": sku.status}
            changes = _changes(db, sku, nz)
            if "名稱" in changes:
                sku.name = nz["product_name"]
            if "規格" in changes:
                sku.variant_name = nz["variant"]
            if "售價" in changes:
                sku.list_price = Decimal(nz["list_price"])
            if "售價幣別" in changes:
                sku.list_currency = nz["list_currency"]
            if "安全庫存" in changes:
                sku.safety_stock_override = nz["safety_stock"]
            if "狀態" in changes:
                sku.status = nz["status"]
            if "分類" in changes:
                db.get(Product, sku.product_id).category_id = category_id(nz["category"])
            db.flush()
            updated += 1
        lid = nz.get("whatnot_listing_id")
        if lid and not db.scalar(select(ExternalSkuMapping.id).where(
                ExternalSkuMapping.platform == "whatnot", ExternalSkuMapping.external_product_id == lid)):
            db.add(ExternalSkuMapping(platform="whatnot", external_product_id=lid, sku_id=sku.id, created_by=actor_id))
            db.flush()
        audit.record(db, action="sku.created" if r.action == "create" else "sku.updated", entity_type="sku",
                     entity_id=sku.id, actor_id=actor_id, before=before,
                     after={"sku_code": sku.sku_code, "name": sku.name, "variant_name": sku.variant_name,
                            "list_price": str(sku.list_price), "safety_stock_override": sku.safety_stock_override,
                            "status": sku.status, "source": f"import:{job_id}"}, ctx=ctx)
    return {"created": created, "updated": updated}
