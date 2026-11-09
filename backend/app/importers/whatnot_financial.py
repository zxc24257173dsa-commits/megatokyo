"""Whatnot seller financial report CSV (seller_financial_report) - fallback when the API cannot be used.

Same file the API produces (see app/integrations/whatnot/financial.py for what is booked). Rows are
keyed by ledger_transaction_id, so a file uploaded twice, or a file overlapping an API sync, never
books anything twice.

Privacy: buyer_name / buyer_state / buyer_country are not stored (removed from the staged rows,
masked in the preview) and the uploaded file is deleted after commit/cancel.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.importers.purchase_cost import RowResult
from app.importers import normalize as n
from app.integrations.whatnot import financial as F
from app.models.finance import PlatformTransaction
from app.models.sales import SalesOrder

PRIVATE_HEADERS = {"buyername", "buyerstate", "buyercountry"}
NOT_KEPT = PRIVATE_HEADERS | {"listingdescription", "costofgoods", "sellerid"}
DELETE_FILE_AFTER = True
_DT = ("completed_at", "order_placed_at")
_DEC = F.MONEY


def _jsonable(v: dict) -> dict:
    return {k: (x.isoformat() if isinstance(x, datetime) else str(x) if isinstance(x, Decimal) else x)
            for k, x in v.items()}


def _restore(v: dict) -> dict:
    out = {k: x for k, x in v.items() if not k.startswith("_")}
    for k in _DT:
        out[k] = datetime.fromisoformat(out[k]) if out.get(k) else None
    for k in _DEC:
        out[k] = Decimal(out[k]) if out.get(k) is not None else None
    return out


def validate(db: Session, rows: list[tuple[int, dict, dict]], job_id: int) -> list[RowResult]:
    results, ids, orders = [], [], []
    for row_number, cells, raw in rows:
        r = RowResult(row_number, {k: v for k, v in raw.items() if n.header_key(k) not in NOT_KEPT})
        results.append(r)
        try:
            parsed = F.parse_row(cells)
        except F.RowError as e:
            r.errors.append(str(e))
            continue
        r.normalized = _jsonable(parsed)
        ids.append(parsed["external_id"])
        if parsed["external_order_id"]:
            orders.append(parsed["external_order_id"])
        t = parsed["transaction_type"]
        if t not in F.KNOWN_TYPES:
            r.warnings.append(f"未知的交易類型 {t}：會保存但不入帳，列入需要確認")
        if parsed["currency"] != "USD":
            r.warnings.append(f"非 USD（{parsed['currency']}）：會保存但不入帳")
    existing = set(db.scalars(select(PlatformTransaction.external_id).where(
        PlatformTransaction.platform == "whatnot", PlatformTransaction.external_id.in_(ids))))
    known_orders = set(db.scalars(select(SalesOrder.external_order_id).where(
        SalesOrder.platform == "whatnot", SalesOrder.external_order_id.in_(orders))))
    for r in results:
        nz = r.normalized
        if r.errors:
            r.action = "error"
        elif nz["external_id"] in existing:
            r.action = "skip_existing"
            r.warnings.append("這筆交易已經在系統中（API 同步或之前的匯入），不重複入帳")
        elif nz["transaction_type"] == "ORDER_EARNINGS" and nz.get("external_order_id") not in known_orders:
            r.action = "import_order_missing"
            r.warnings.append("ERP 沒有這筆訂單：先保存，訂單進來後自動補入手續費")
        else:
            r.action = "import"
        if nz.get("transaction_type") == "ORDER_EARNINGS" and not r.errors:
            fees = sum(Decimal(nz.get(k) or 0) for k in ("commission_fee", "commission_tax", "processing_fee",
                                                          "processing_fee_tax"))
            nz["_fees"] = str(fees)
    return results


def commit(db: Session, *, actor_id: int, job_id: int, results: list[RowResult], create_missing_skus: bool,
           ctx, **_) -> dict:
    todo = [r for r in results if r.action in ("import", "import_order_missing")]
    if not todo:
        raise AppError("nothing_to_import", "There are no new transactions to import", 422)
    stats = {"transactions": 0, "booked": 0, "order_not_in_erp": 0, "needs_review": 0, "fees_usd": Decimal(0),
             "refunds_usd": Decimal(0)}
    for r in todo:
        pt, _ = F.store(db, _restore(r.normalized), source="csv", import_job_id=job_id)
        stats["transactions"] += 1
        stats[pt.status] += 1
        if pt.status == "booked" and pt.transaction_type == "ORDER_EARNINGS":
            stats["fees_usd"] += Decimal(r.normalized.get("_fees") or 0) + (pt.seller_shipping_charge or 0)
        if pt.transaction_type == "ORDER_REFUND":
            stats["refunds_usd"] += pt.amount
    stats["fees_usd"] = str(stats["fees_usd"].quantize(Decimal("0.01")))
    stats["refunds_usd"] = str(stats["refunds_usd"].quantize(Decimal("0.01")))
    return stats
