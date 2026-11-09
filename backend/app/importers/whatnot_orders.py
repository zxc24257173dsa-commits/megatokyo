"""Whatnot show order export (CSV "live-<show uuid>.csv") - fallback when the API is unavailable.

Verified against a real export (2026-11-09): one row per order item; `order_numeric_id` is the
same id the Seller API uses (so CSV and API never double count), `placed_at` is UTC, the file
name carries the show id, `sku` is usually empty (listings are matched by title rules).

Privacy: buyer username, shipping address, postal code and gift recipient are NOT stored -
they are removed from the staged rows, and the uploaded file is deleted after commit/cancel.
"""
import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.importers import normalize as n
from app.importers.purchase_cost import RowResult
from app.integrations.whatnot import processing as P
from app.models.imports import ImportJob
from app.models.sales import SalesOrder

PRIVATE_HEADERS = {"buyerusername", "shippingaddress", "postalcode", "giftedto", "buyername", "buyeremail",
                   "recipient", "address"}
DELETE_FILE_AFTER = True
SHOW_RE = re.compile(r"live-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def show_id_from_filename(name: str) -> str | None:
    m = SHOW_RE.search(name or "")
    return m.group(1).lower() if m else None


def _parse_ts(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    s = n.text(v)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # Whatnot exports are UTC


def validate(db: Session, rows: list[tuple[int, dict, dict]], job_id: int) -> list[RowResult]:
    results = []
    order_ids = []
    for row_number, cells, raw in rows:
        r = RowResult(row_number, {k: v for k, v in raw.items() if n.header_key(k) not in PRIVATE_HEADERS})
        results.append(r)
        g, nz = cells.get, r.normalized
        oid = n.text(g("order_id"))
        if not oid or not oid.isdigit():
            r.errors.append("缺少或無效的訂單編號（order_numeric_id）")
        nz["order_id"] = oid
        nz["title"] = n.text(g("title"))
        if not nz["title"]:
            r.errors.append("缺少商品名稱")
        nz["sku"] = n.text(g("sku"))
        qty, err = n.integer(g("qty"))
        if err or qty is None or qty <= 0:
            r.errors.append(f"數量：{err or '必須大於 0'}")
        nz["qty"] = qty
        price, err = n.decimal(g("price"), allow_blank=False)
        if err or price is None or price < 0:
            r.errors.append(f"價格：{err or '不能是負數'}")
        else:
            nz["price"] = str(price)
            if price == 0:
                r.warnings.append("$0：贈品（giveaway），不計營收，但商品成本會計入")
        placed = _parse_ts(g("placed_at"))
        nz["placed_at"] = placed.isoformat() if placed else None
        flag = (n.text(g("status_flag")) or "").lower()
        nz["status"] = {"failed": "failed", "cancelled": "cancelled"}.get(flag, "pending_fulfillment")
        if flag and flag not in ("failed", "cancelled"):
            r.warnings.append(f"未知的狀態「{flag}」，當作已付款")
        if nz["status"] != "pending_fulfillment":
            r.warnings.append("付款失敗" if nz["status"] == "failed" else "已取消")
        elif not placed:
            r.errors.append("已付款的訂單缺少下單時間（placed_at）")
        nz["shipment_id"] = n.text(g("shipment_id"))
        if oid:
            order_ids.append(oid)
        if not r.errors and nz["status"] == "pending_fulfillment":
            sid = P.resolve_sku(db, {"purchased_item": {"title": nz["title"], "sku": nz["sku"]}})
            r.matched_sku_id = sid
            if sid is None:
                r.warnings.append("沒有對應的 SKU：匯入後到「訂單 › 待對應商品」對應一次，同名商品會自動套用")

    existing = {o.external_order_id: o.source for o in db.scalars(
        select(SalesOrder).where(SalesOrder.platform == "whatnot", SalesOrder.external_order_id.in_(order_ids)))}
    for r in results:
        if r.errors:
            r.action = "error"
        elif r.normalized.get("order_id") in existing:
            r.action = "skip_existing"
            r.warnings.append("這筆訂單已在系統中（API 或之前的匯入），不重複匯入")
        elif r.normalized["status"] != "pending_fulfillment":
            r.action = "import_unpaid"
        else:
            r.action = "import" if r.matched_sku_id else "import_unmapped"
    return results


def commit(db: Session, *, actor_id: int, job_id: int, results: list[RowResult], create_missing_skus: bool,
           ctx, mark_shipped: bool = False, **_) -> dict:
    todo = [r for r in results if r.action in ("import", "import_unmapped", "import_unpaid")]
    if not todo:
        raise AppError("nothing_to_import", "There are no new orders to import", 422)
    job = db.get(ImportJob, job_id)
    show_id = show_id_from_filename(job.filename)
    orders: dict[str, list[RowResult]] = {}
    for r in todo:
        orders.setdefault(r.normalized["order_id"], []).append(r)
    lines = revenue = unmapped = 0
    for oid, rows in orders.items():
        first = rows[0].normalized
        status = first["status"]
        if status == "pending_fulfillment" and mark_shipped:
            status = "completed"
        subtotal = sum(Decimal(r.normalized["price"]) * r.normalized["qty"] for r in rows)
        placed = first["placed_at"] or datetime.now(UTC).isoformat()
        order = {"id": oid, "status": status, "show_id": show_id,
                 "transaction_type": "giveaway" if subtotal == 0 else None,
                 "amounts": {"subtotal": {"amount": str(subtotal), "currency": "USD"},
                             "shipping": None, "tax": None, "applied_discount": None,
                             "total": {"amount": str(subtotal), "currency": "USD"}},
                 "created_at": placed, "updated_at": placed, "cancelled_at": None}
        items = [{"id": f"csv-{oid}-{i}", "order_id": oid, "product_id": None, "variant_id": None,
                  "product_external_id": None, "variant_external_id": None,
                  "purchased_item": {"title": r.normalized["title"], "description": None, "sku": r.normalized["sku"]},
                  "quantity": r.normalized["qty"], "unit_price": {"amount": r.normalized["price"], "currency": "USD"},
                  "shipment_id": None} for i, r in enumerate(rows, start=1)]
        P.process_order(db, order, items, source="csv")
        lines += len(items)
        if status in P.PAID:
            revenue += subtotal
            unmapped += sum(1 for r in rows if r.action == "import_unmapped")
    return {"orders": len(orders), "lines": lines, "revenue_usd": str(revenue.quantize(Decimal("0.01"))),
            "unmapped_lines": unmapped, "show_id": show_id, "marked_shipped": mark_shipped}
