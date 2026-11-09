"""Purchase-cost sheet: validation and commit.

Rules
  * Example / sample rows are detected and skipped automatically.
  * Exchange rate is written "1 USD = N <currency>" (owner: 1 USD = 150 JPY). It is never
    guessed: a non-USD row without a rate is an error; the inverted form (0.0067) is rejected
    with the correct value. Amounts are converted by DIVIDING by the rate.
  * Shipping (Japan domestic + international) is in JPY; duty / other costs are in USD.
  * Cost formulas are recomputed here; numbers typed in the sheet are only cross-checked.
  * A batch ID that already exists in the ERP is rejected (history is never overwritten).
  * Importing records COST ONLY. Stock appears only through explicit "ship from Japan" (in
    transit) and "receive in the US" actions, so past batches already sold don't become
    phantom stock.
"""
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.importers import normalize as n
from app.models.catalog import Product, Sku, Supplier
from app.models.purchasing import PurchaseBatch, PurchaseLine
from app.services import audit
from app.services.audit import RequestContext

EXAMPLE_RE = re.compile(r"example|sample.*replace|replace before use|示例|範例|範本", re.I)
# Plausible "units per 1 USD" ranges; anything outside is rejected rather than guessed.
FX_RANGES = {"JPY": (Decimal("70"), Decimal("300")), "TWD": (Decimal("20"), Decimal("50"))}
CENT = Decimal("0.01")
TOLERANCE = Decimal("0.01")


@dataclass
class RowResult:
    row_number: int
    raw: dict
    normalized: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    action: str = "import"
    matched_sku_id: int | None = None


def _q(d: Decimal, places: str) -> Decimal:
    return d.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _check_fx(cur: str, quote: Decimal | None, r: RowResult) -> Decimal | None:
    """Returns the rate as "units of `cur` per 1 USD"."""
    if cur == "USD":
        if quote is not None and quote != 1:
            r.errors.append(f"幣別是 USD，匯率應為 1（目前 {quote}）")
            return None
        return Decimal(1)
    if quote is None:
        r.errors.append(f"{cur} 需要填寫匯率（1 USD = ? {cur}）；系統不會自行假設匯率")
        return None
    if quote <= 0:
        r.errors.append("匯率必須大於 0")
        return None
    lo, hi = FX_RANGES[cur]
    if not lo <= quote <= hi:
        inverse = _q(Decimal(1) / quote, "0.01") if quote < 1 else None
        if inverse is not None and lo <= inverse <= hi:
            r.errors.append(f"匯率看起來是「1 {cur} = {quote} USD」的寫法；這裡要填「1 USD = ? {cur}」，約 {inverse}")
        else:
            r.errors.append(f"{cur} 匯率 {quote} 不在合理範圍（1 USD = {lo}–{hi} {cur}）")
        return None
    return quote


def validate(db: Session, rows: list[tuple[int, dict, dict]], job_id: int) -> list[RowResult]:
    results: list[RowResult] = []
    today = datetime.now(UTC).date()
    for row_number, cells, raw in rows:
        r = RowResult(row_number, raw)
        results.append(r)
        g = cells.get
        if any(EXAMPLE_RE.search(n.text(g(k)) or "") for k in ("batch_ref", "product_name", "notes")):
            r.action = "skip_example"
            r.warnings.append("範例列，不會匯入")
            continue

        nz = r.normalized
        nz["batch_ref"] = n.text(g("batch_ref"))
        nz["sku_code"] = n.text(g("sku_code"))
        nz["product_name"] = n.text(g("product_name"))
        nz["supplier"] = n.text(g("supplier"))
        nz["notes"] = n.text(g("notes"))
        if not nz["sku_code"]:
            r.errors.append("缺少 SKU")
        d, err = n.parse_date(g("purchase_date"))
        if err:
            r.errors.append(f"採購日期：{err}")
        elif d is None:
            r.errors.append("缺少採購日期")
        else:
            nz["purchase_date"] = d.isoformat()
            if d > today:
                r.warnings.append("採購日期在未來")
            elif d < today - timedelta(days=3 * 365):
                r.warnings.append("採購日期超過 3 年前")
        qty, err = n.integer(g("qty"))
        if err:
            r.errors.append(f"採購數量：{err}")
        elif qty is not None and qty <= 0:
            r.errors.append("採購數量必須大於 0")
        else:
            nz["qty"] = qty
        unit, err = n.decimal(g("unit_cost"), allow_blank=False)
        if err:
            r.errors.append(f"採購單價：{err}")
        elif unit < 0:
            r.errors.append("採購單價不能是負數")
        else:
            nz["unit_cost"] = str(unit)
        cur, err = n.currency(g("currency"))
        if err:
            r.errors.append(err)
        elif cur is None:
            r.errors.append("缺少採購幣別")
        fx_raw, err = n.decimal(g("fx_rate"))
        if err:
            r.errors.append(f"匯率：{err}")
        fx = _check_fx(cur, fx_raw, r) if cur and not err else None
        if cur:
            nz["currency"] = cur
        if fx is not None:
            nz["fx_quote"] = str(fx)

        amounts = {}
        for key, label in (("domestic_shipping_jpy", "日本境內運費（日幣）"), ("intl_freight_jpy", "國際運費（日幣）"),
                           ("domestic_shipping_usd", "日本境內運費（USD）"), ("intl_freight_usd", "國際運費（USD）"),
                           ("duty_other_usd", "關稅/其他費用")):
            v, err = n.decimal(g(key))
            if err:
                r.errors.append(f"{label}：{err}")
            elif v is not None and v < 0:
                r.errors.append(f"{label}不能是負數")
            else:
                amounts[key] = v or Decimal(0)
        jpy_shipping = amounts.get("domestic_shipping_jpy", 0) + amounts.get("intl_freight_jpy", 0)
        if jpy_shipping and cur and cur != "JPY":
            r.errors.append("運費是日幣，但這列的採購幣別不是 JPY，沒有日幣匯率可以換算；請分開填寫或改用 USD 運費欄")

        method = n.text(g("costing_method"))
        if method and method.upper() != "FIFO":
            r.warnings.append(f"試算表寫「{method}」；系統統一使用 FIFO（先進先出）計算銷貨成本")

        if not r.errors:
            purchase = _q(Decimal(qty) * unit / fx, "0.0001")
            dom = _q(amounts["domestic_shipping_jpy"] / fx + amounts["domestic_shipping_usd"], "0.0001")
            intl = _q(amounts["intl_freight_jpy"] / fx + amounts["intl_freight_usd"], "0.0001")
            duty = _q(amounts["duty_other_usd"], "0.0001")
            total = purchase + dom + intl + duty
            per_unit = _q(total / qty, "0.000001")
            nz.update(
                fx_quote=str(fx), fx_rate=str(_q(Decimal(1) / fx, "0.00000001")),
                domestic_shipping_jpy=str(amounts["domestic_shipping_jpy"]),
                intl_freight_jpy=str(amounts["intl_freight_jpy"]),
                domestic_shipping_usd=str(dom), intl_freight_usd=str(intl), duty_other_usd=str(duty),
                purchase_cost_usd=str(purchase), landed_total_usd=str(_q(total, "0.0001")), landed_unit_usd=str(per_unit),
            )
            if dom + intl > purchase > 0:
                r.warnings.append(f"運費（USD {_q(dom + intl, '0.01')}）比商品採購成本（USD {_q(purchase, '0.01')}）還高，"
                                  "請確認運費欄的幣別是否正確")
            for key, computed, label in (("purchase_cost_usd", purchase, "採購成本"),
                                         ("total_cost_usd", total, "總成本"),
                                         ("landed_unit_usd", per_unit, "每單位落地成本")):
                given, err = n.decimal(g(key))
                if err or given is None:
                    continue  # uncalculated formulas / blanks: nothing to cross-check
                if abs(given - computed) > TOLERANCE:
                    r.warnings.append(f"{label}：試算表 {given}，系統計算 {_q(computed, '0.01')}（以系統計算為準）")

    _cross_row_checks(db, results, job_id)
    return results


def _cross_row_checks(db: Session, results: list[RowResult], job_id: int) -> None:
    active = [r for r in results if r.action != "skip_example"]
    # SKU matching (case-insensitive)
    codes = {r.normalized.get("sku_code").upper() for r in active if r.normalized.get("sku_code")}
    existing = {code.upper(): (sid, is_bundle) for sid, code, is_bundle in
                db.execute(select(Sku.id, Sku.sku_code, Sku.is_bundle).where(func.upper(Sku.sku_code).in_(codes)))}
    for r in active:
        code = (r.normalized.get("sku_code") or "").upper()
        if not code:
            continue
        if code in existing:
            sid, is_bundle = existing[code]
            if is_bundle:
                r.errors.append("組合商品不能直接採購；請採購它的組成 SKU")
            r.matched_sku_id = sid
        else:
            r.action = "import_new_sku"
            if r.normalized.get("product_name"):
                r.warnings.append(f"系統沒有 SKU {r.normalized['sku_code']}，確認匯入時會一起建立新商品")
            else:
                r.errors.append("系統沒有這個 SKU，且缺少商品名稱，無法建立")

    # Batch grouping: blank batch ID -> one batch per row.
    for r in active:
        r.normalized["batch_no"] = r.normalized.get("batch_ref") or f"IMP{job_id}-R{r.row_number}"
    groups: dict[str, list[RowResult]] = {}
    for r in active:
        groups.setdefault(r.normalized["batch_no"].upper(), []).append(r)
    taken = {b.upper() for b in db.scalars(select(PurchaseBatch.batch_no)
                                            .where(func.upper(PurchaseBatch.batch_no).in_(groups)))}
    for key, rows in groups.items():
        if key in taken:
            for r in rows:
                r.errors.append(f"批次 {r.normalized['batch_no']} 已經匯入過（歷史成本不會被覆蓋）")
        dates = {r.normalized.get("purchase_date") for r in rows if r.normalized.get("purchase_date")}
        suppliers = {(r.normalized.get("supplier") or "").lower() for r in rows}
        if len(dates) > 1 or len(suppliers) > 1:
            for r in rows:
                r.errors.append("同一個批次編號的列，採購日期與供應商必須相同")
        seen = set()
        for r in rows:
            code = (r.normalized.get("sku_code") or "").upper()
            if code in seen:
                r.errors.append("同一個批次裡 SKU 重複出現")
            seen.add(code)

    for r in active:
        if r.errors:
            r.action = "error"


def commit(db: Session, *, actor_id: int, job_id: int, results: list[RowResult], create_missing_skus: bool,
           ctx: RequestContext) -> dict:
    todo = [r for r in results if r.action in ("import", "import_new_sku")]
    if not todo:
        raise AppError("nothing_to_import", "There are no valid rows to import", 422)
    if any(r.action == "import_new_sku" for r in todo) and not create_missing_skus:
        raise AppError("new_skus_not_confirmed", "Some SKUs don't exist yet; confirm creating them", 422,
                       {"sku_codes": sorted({r.normalized["sku_code"] for r in todo if r.action == "import_new_sku"})})

    new_skus: dict[str, int] = {}
    for r in todo:
        if r.action == "import_new_sku":
            code = r.normalized["sku_code"]
            if code.upper() not in new_skus:
                p = Product(name=r.normalized["product_name"])
                db.add(p)
                db.flush()
                sku = Sku(product_id=p.id, sku_code=code, name=r.normalized["product_name"])
                db.add(sku)
                db.flush()
                new_skus[code.upper()] = sku.id
                audit.record(db, action="sku.created", entity_type="sku", entity_id=sku.id, actor_id=actor_id,
                             after={"sku_code": code, "name": p.name, "source": f"import:{job_id}"}, ctx=ctx)
            r.matched_sku_id = new_skus[code.upper()]

    suppliers: dict[str, int] = {}

    def supplier_id(name: str | None) -> int | None:
        if not name:
            return None
        key = name.lower()
        if key not in suppliers:
            sid = db.scalar(select(Supplier.id).where(func.lower(Supplier.name) == key))
            if sid is None:
                s = Supplier(name=name)
                db.add(s)
                db.flush()
                sid = s.id
            suppliers[key] = sid
        return suppliers[key]

    groups: dict[str, list[RowResult]] = {}
    for r in todo:
        groups.setdefault(r.normalized["batch_no"], []).append(r)
    now = datetime.now(UTC)
    batch_ids, line_count, total_usd = [], 0, Decimal(0)
    for batch_no, rows in groups.items():
        first = rows[0].normalized
        batch = PurchaseBatch(batch_no=batch_no, supplier_id=supplier_id(first.get("supplier")),
                              purchase_date=date.fromisoformat(first["purchase_date"]), import_job_id=job_id,
                              created_by=actor_id)
        db.add(batch)
        db.flush()
        for i, r in enumerate(rows, start=1):
            nz = r.normalized
            db.add(PurchaseLine(
                batch_id=batch.id, line_no=i, sku_id=r.matched_sku_id, qty=nz["qty"],
                unit_price_orig=Decimal(nz["unit_cost"]), currency_orig=nz["currency"],
                fx_rate_to_base=Decimal(nz["fx_rate"]), fx_rate_quote=Decimal(nz["fx_quote"]),
                fx_rate_date=date.fromisoformat(nz["purchase_date"]), fx_source="cost sheet",
                purchase_cost_base=Decimal(nz["purchase_cost_usd"]),
                domestic_shipping_jpy=Decimal(nz["domestic_shipping_jpy"]), intl_freight_jpy=Decimal(nz["intl_freight_jpy"]),
                domestic_shipping_base=Decimal(nz["domestic_shipping_usd"]),
                intl_freight_base=Decimal(nz["intl_freight_usd"]), duty_other_base=Decimal(nz["duty_other_usd"]),
                landed_total_base=Decimal(nz["landed_total_usd"]), landed_unit_cost_base=Decimal(nz["landed_unit_usd"]),
                note=nz.get("notes"), source_import_row_id=nz.get("_import_row_id"),
            ))
            line_count += 1
            total_usd += Decimal(nz["landed_total_usd"])
        db.flush()
        batch.status, batch.confirmed_by, batch.confirmed_at = "confirmed", actor_id, now  # frozen from here on
        db.flush()
        batch_ids.append(batch.id)
        audit.record(db, action="purchase_batch.imported", entity_type="purchase_batch", entity_id=batch.id,
                     actor_id=actor_id, after={"batch_no": batch_no, "lines": len(rows), "import_job_id": job_id},
                     ctx=ctx)
    return {"batches": len(batch_ids), "lines": line_count, "new_skus": len(new_skus),
            "landed_total_usd": str(_q(total_usd, "0.01"))}
