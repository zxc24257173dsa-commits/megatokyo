"""Read models for the catalog / inventory screens. Costs are only returned to users allowed to see them."""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import storage

COST_PERMISSIONS = {"finance.view", "import.cost"}


def can_view_cost(permissions: set[str]) -> bool:
    return bool(permissions & COST_PERMISSIONS)


def sku_rows(db: Session, *, search: str | None = None, category_id: int | None = None, status: str | None = "active",
             include_bundles: bool = True, with_cost: bool = False, sku_id: int | None = None) -> list[dict]:
    params: dict = {}
    where = ["TRUE"]
    if sku_id is not None:
        where.append("s.id = :sid")
        params["sid"] = sku_id
    if search:
        where.append("(s.sku_code ILIKE :q OR s.name ILIKE :q OR p.name ILIKE :q OR s.barcode = :raw)")
        params["q"] = "%" + search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        params["raw"] = search
    if category_id is not None:
        where.append("p.category_id = :cat")
        params["cat"] = category_id
    if status:
        where.append("s.status = :status")
        params["status"] = status
    if not include_bundles:
        where.append("NOT s.is_bundle")
    rows = db.execute(text(f"""
        SELECT s.id, s.sku_code, s.name, s.variant_name, s.is_bundle, s.status, s.version, s.list_price,
               s.list_currency, s.safety_stock_override, p.id AS product_id, p.name AS product_name,
               c.id AS category_id, c.name AS category_name,
               COALESCE(v.jp_on_hand, 0) AS jp_on_hand, COALESCE(v.in_transit, 0) AS in_transit,
               COALESCE(v.us_on_hand, 0) AS us_on_hand, COALESCE(v.us_reserved, 0) AS us_reserved,
               COALESCE(v.us_available, 0) AS us_available,
               (SELECT max(l.received_at) FROM inventory_lots l WHERE l.sku_id = s.id) AS last_received_at,
               (SELECT i.storage_key FROM sku_images i WHERE i.sku_id = s.id AND i.is_primary) AS image_key,
               (SELECT COALESCE(sum(val.value_base), 0) FROM v_inventory_valuation val
                 WHERE val.sku_id = s.id) AS inventory_value_usd
          FROM skus s
          JOIN products p ON p.id = s.product_id
          LEFT JOIN categories c ON c.id = p.category_id
          LEFT JOIN v_inventory_position v ON v.sku_id = s.id
         WHERE {' AND '.join(where)}
         ORDER BY s.sku_code
    """), params).mappings().all()
    out = [dict(r) for r in rows]
    _fill_bundle_availability(db, out)
    for r in out:
        r["image_url"] = storage.signed_url(r.pop("image_key")) if r["image_key"] else None
        if not with_cost:
            r.pop("inventory_value_usd")
    return out


def _fill_bundle_availability(db: Session, rows: list[dict]) -> None:
    """A bundle's stock = how many complete sets the components allow (per location)."""
    bundle_ids = [r["id"] for r in rows if r["is_bundle"]]
    if not bundle_ids:
        return
    comps = db.execute(text("""
        SELECT b.bundle_sku_id, b.qty, COALESCE(v.jp_on_hand, 0) jp, COALESCE(v.in_transit, 0) tr,
               COALESCE(v.us_on_hand, 0) us, COALESCE(v.us_available, 0) av
          FROM sku_bundle_components b LEFT JOIN v_inventory_position v ON v.sku_id = b.component_sku_id
         WHERE b.bundle_sku_id = ANY(:ids)
    """), {"ids": bundle_ids}).mappings().all()
    by_bundle: dict[int, list] = {}
    for c in comps:
        by_bundle.setdefault(c["bundle_sku_id"], []).append(c)
    for r in rows:
        if not r["is_bundle"]:
            continue
        cs = by_bundle.get(r["id"], [])
        sets = (lambda key: min((c[key] // c["qty"] for c in cs), default=0))
        r.update(jp_on_hand=sets("jp"), in_transit=sets("tr"), us_on_hand=sets("us"), us_available=sets("av"),
                 us_reserved=0)


def inventory_summary(db: Session, *, with_cost: bool) -> list[dict]:
    rows = db.execute(text("""
        SELECT loc.code AS location_code, loc.name AS location_name,
               COALESCE(sum(val.qty), 0)::int AS units, COALESCE(sum(val.value_base), 0) AS value_usd,
               count(DISTINCT val.sku_id) AS sku_count
          FROM locations loc LEFT JOIN v_inventory_valuation val ON val.location_code = loc.code
         WHERE loc.kind <> 'virtual'
         GROUP BY loc.id ORDER BY loc.id
    """)).mappings().all()
    reserved = db.execute(text("SELECT COALESCE(sum(us_reserved), 0) FROM v_inventory_position")).scalar_one()
    out = []
    for r in rows:
        d = dict(r)
        if not with_cost:
            d.pop("value_usd")
        if d["location_code"] == "US_MAIN":
            d["reserved_units"] = int(reserved)
            d["available_units"] = d["units"] - int(reserved)
        out.append(d)
    return out


def lots(db: Session, sku_id: int, *, with_cost: bool) -> list[dict]:
    rows = db.execute(text("""
        SELECT l.id, l.lot_no, l.source, l.received_at, l.original_qty,
               l.original_unit_cost_base, c.unit_cost_base,
               COALESCE(sum(b.qty) FILTER (WHERE loc.code = 'JP_MAIN'), 0)::int AS jp,
               COALESCE(sum(b.qty) FILTER (WHERE loc.code = 'IN_TRANSIT'), 0)::int AS in_transit,
               COALESCE(sum(b.qty) FILTER (WHERE loc.code = 'US_MAIN'), 0)::int AS us
          FROM inventory_lots l
          JOIN v_lot_cost c ON c.lot_id = l.id
          LEFT JOIN inventory_balances b ON b.lot_id = l.id
          LEFT JOIN locations loc ON loc.id = b.location_id
         WHERE l.sku_id = :s
         GROUP BY l.id, c.unit_cost_base
         ORDER BY l.received_at, l.id
    """), {"s": sku_id}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["remaining"] = d["jp"] + d["in_transit"] + d["us"]
        if not with_cost:
            d.pop("original_unit_cost_base")
            d.pop("unit_cost_base")
        out.append(d)
    return out


def movements(db: Session, *, sku_id: int | None = None, location_code: str | None = None,
              movement_type: str | None = None, before_id: int | None = None, limit: int = 50) -> tuple[list[dict], int | None]:
    where, params = ["TRUE"], {"limit": limit + 1}
    if sku_id is not None:
        where.append("m.sku_id = :sku")
        params["sku"] = sku_id
    if location_code:
        where.append("(fl.code = :loc OR tl.code = :loc)")
        params["loc"] = location_code
    if movement_type:
        where.append("m.movement_type = :mt")
        params["mt"] = movement_type
    if before_id is not None:
        where.append("m.id < :before")
        params["before"] = before_id
    rows = db.execute(text(f"""
        SELECT m.id, m.occurred_at, m.movement_type, m.qty, fl.code AS from_location, tl.code AS to_location,
               s.id AS sku_id, s.sku_code, l.lot_no, m.ref_type, m.ref_id, m.note, u.display_name AS created_by
          FROM inventory_movements m
          JOIN locations fl ON fl.id = m.from_location_id
          JOIN locations tl ON tl.id = m.to_location_id
          JOIN skus s ON s.id = m.sku_id
          JOIN inventory_lots l ON l.id = m.lot_id
          LEFT JOIN users u ON u.id = m.created_by
         WHERE {' AND '.join(where)}
         ORDER BY m.id DESC LIMIT :limit
    """), params).mappings().all()
    items = [dict(r) for r in rows[:limit]]
    return items, (items[-1]["id"] if len(rows) > limit else None)


def money(v) -> str | None:
    return None if v is None else str(Decimal(v).quantize(Decimal("0.01")))
