"""Field definitions per import type and automatic sheet / header-row / column detection.

Headers are compared after normalisation (case, spaces, punctuation, full-width ignored).
Exact synonym matches win over partial ones; each column is used at most once. The user can
always correct the suggested mapping in the preview screen, and the mapping is remembered
per header layout (import_mapping_templates) for next time.
"""
from dataclasses import dataclass

from app.importers.normalize import header_key, is_blank_row
from app.importers.reader import Sheet


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    required: bool
    synonyms: tuple[str, ...]


PURCHASE_COST_FIELDS = (
    FieldDef("batch_ref", "採購批次編號", False, ("cost batch id", "batch id", "batch no", "lot id", "lot", "批次編號", "採購批次", "批次", "ロット")),
    FieldDef("sku_code", "SKU", True, ("sku", "sku code", "sku編號", "貨號", "品番", "商品コード")),
    FieldDef("product_name", "商品名稱", False, ("product name", "item name", "商品名稱", "品名", "商品名")),
    FieldDef("purchase_date", "採購日期", True, ("purchase date", "date", "採購日期", "進貨日期", "購入日", "仕入日")),
    FieldDef("qty", "採購數量", True, ("purchase quantity", "quantity", "qty", "採購數量", "數量", "数量")),
    FieldDef("unit_cost", "採購單價（原幣）", True, ("unit purchase cost", "unit cost", "unit price", "採購單價", "單價", "単価", "仕入単価")),
    FieldDef("currency", "採購幣別", True, ("purchase currency", "currency", "採購幣別", "幣別", "通貨")),
    FieldDef("fx_rate", "匯率（1 USD = ? 原幣，例：150）", False, ("exchange rate to usd", "exchange rate", "fx rate", "usd jpy", "jpy per usd", "匯率", "為替レート")),
    FieldDef("purchase_cost_usd", "採購成本 USD（核對用）", False, ("purchase cost usd", "purchase cost", "採購成本usd", "採購成本")),
    # Shipping is paid in yen (owner, 2026-11-09). A header that explicitly says USD maps to the USD variant.
    FieldDef("domestic_shipping_jpy", "日本境內運費（日幣）", False, ("domestic japan shipping jpy", "domestic japan shipping", "domestic shipping", "japan shipping", "日本境內運費", "國內運費", "国内送料")),
    FieldDef("intl_freight_jpy", "國際運費（日幣）", False, ("international freight allocation jpy", "international freight allocation", "international freight", "intl freight", "國際運費", "国際送料")),
    FieldDef("domestic_shipping_usd", "日本境內運費（USD，標題註明 USD 時）", False, ("domestic japan shipping usd",)),
    FieldDef("intl_freight_usd", "國際運費（USD，標題註明 USD 時）", False, ("international freight allocation usd",)),
    FieldDef("duty_other_usd", "關稅/其他費用 USD", False, ("duties other costs usd", "duties other costs", "duties", "duty", "other costs", "關稅", "其他費用", "関税")),
    FieldDef("total_cost_usd", "總成本 USD（核對用）", False, ("total batch cost usd", "total batch cost", "total cost", "總成本")),
    FieldDef("landed_unit_usd", "每單位落地成本 USD（核對用）", False, ("landed cost per unit usd", "landed cost per unit", "landed cost", "商品落地成本", "落地成本")),
    FieldDef("costing_method", "成本方法", False, ("costing method fifo weighted average", "costing method", "成本方法")),
    FieldDef("supplier", "供應商", False, ("supplier", "vendor", "供應商", "仕入先")),
    FieldDef("notes", "備註", False, ("notes", "note", "remarks", "備註", "メモ")),
)

SKU_MASTER_FIELDS = (
    FieldDef("sku_code", "SKU", True, ("sku", "sku code", "sku編號", "貨號", "品番", "商品コード")),
    FieldDef("product_name", "商品名稱", True, ("product name", "item name", "商品名稱", "品名", "商品名")),
    FieldDef("category", "分類", False, ("category", "商品分類", "分類", "カテゴリ")),
    FieldDef("variant", "規格", False, ("variant specification", "variant", "specification", "規格", "商品規格")),
    FieldDef("whatnot_listing_id", "Whatnot Listing ID", False, ("whatnot listing id", "listing id")),
    FieldDef("list_price", "售價", False, ("default selling price", "selling price", "price", "售價", "販売価格")),
    FieldDef("list_currency", "售價幣別", False, ("selling currency", "售價幣別")),
    FieldDef("safety_stock", "安全庫存", False, ("safety stock", "安全庫存", "安全在庫")),
    FieldDef("status", "狀態", False, ("product status", "status", "狀態")),
    FieldDef("notes", "備註", False, ("notes", "note", "備註")),
)

WHATNOT_ORDER_FIELDS = (
    FieldDef("order_id", "訂單編號（order_numeric_id）", True, ("order numeric id",)),
    FieldDef("title", "商品名稱", True, ("product name", "listing title", "title")),
    FieldDef("qty", "數量", True, ("product quantity", "quantity", "qty")),
    FieldDef("price", "成交價", True, ("original item price", "item price", "sold price", "price")),
    FieldDef("placed_at", "下單時間（UTC）", False, ("placed at", "order date", "created at")),
    FieldDef("status_flag", "取消／失敗", False, ("cancelled or failed", "status")),
    FieldDef("shipment_id", "Whatnot 出貨編號", False, ("shipment id",)),
    FieldDef("sku", "SKU", False, ("sku",)),
)

WHATNOT_FINANCIAL_FIELDS = (
    FieldDef("transaction_completed_at_utc", "交易完成時間（UTC）", True, ("transaction completed at utc",)),
    FieldDef("order_placed_at_utc", "下單時間", False, ("order placed at utc",)),
    FieldDef("transaction_type", "交易類型", True, ("transaction type",)),
    FieldDef("transaction_message", "說明", False, ("transaction message",)),
    FieldDef("order_id", "訂單編號", False, ("order id",)),
    FieldDef("listing_title", "商品名稱", False, ("listing title",)),
    FieldDef("buy_format", "銷售方式", False, ("buy format",)),
    FieldDef("sale_type", "銷售類型", False, ("sale type",)),
    FieldDef("quantity_sold", "數量", False, ("quantity sold",)),
    FieldDef("sku", "SKU", False, ("sku",)),
    FieldDef("livestream_id", "直播編號", False, ("livestream id",)),
    FieldDef("shipment_id", "出貨編號", False, ("shipment id",)),
    FieldDef("transaction_currency", "幣別", False, ("transaction currency",)),
    FieldDef("transaction_amount", "交易金額", True, ("transaction amount",)),
    FieldDef("buyer_paid", "買家支付", False, ("buyer paid",)),
    FieldDef("original_item_price", "原價", False, ("original item price",)),
    FieldDef("coupon_cost", "優惠券", False, ("coupon cost",)),
    FieldDef("post_coupon_price", "成交價（扣優惠券後）", False, ("post coupon price",)),
    FieldDef("seller_shipping_charge", "賣家支付運費", False, ("seller shipping charge",)),
    FieldDef("commission_fee", "平台佣金", False, ("commission fee",)),
    FieldDef("commission_tax", "佣金稅", False, ("commission tax",)),
    FieldDef("processing_fee", "金流手續費", False, ("processing fee",)),
    FieldDef("processing_fee_tax", "手續費稅", False, ("processing fee tax",)),
    FieldDef("ledger_transaction_id", "交易編號（ledger_transaction_id）", True, ("ledger transaction id",)),
)

# Columns the template has but we deliberately never import (the ERP computes / owns these).
IGNORED_SKU_MASTER_COLUMNS = ("current standard cost usd", "japan inventory", "us inventory", "in transit inventory",
                              "reserved inventory", "available us inventory")

FIELDS = {"purchase_cost": PURCHASE_COST_FIELDS, "sku_master": SKU_MASTER_FIELDS,
          "whatnot_orders": WHATNOT_ORDER_FIELDS, "whatnot_financial": WHATNOT_FINANCIAL_FIELDS}
HEADER_SCAN_ROWS = 15


def suggest_columns(headers: list, fields: tuple[FieldDef, ...]) -> tuple[dict[str, int], int]:
    """Returns ({field_key: column_index}, score)."""
    keys = [header_key(h) for h in headers]
    candidates = []  # (strength, synonym_len, field_key, col)
    for f in fields:
        syns = [header_key(s) for s in f.synonyms]
        for col, k in enumerate(keys):
            if not k:
                continue
            for syn in syns:
                if k == syn:
                    candidates.append((2, len(syn), f.key, col))
                elif len(syn) >= 3 and (k.startswith(syn) or syn in k):
                    candidates.append((1, len(syn), f.key, col))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    mapping: dict[str, int] = {}
    used_cols: set[int] = set()
    score = 0
    for strength, _, key, col in candidates:
        if key in mapping or col in used_cols:
            continue
        mapping[key] = col
        used_cols.add(col)
        score += strength
    return mapping, score


def detect(sheets: list[Sheet], import_type: str) -> dict | None:
    """Pick the sheet + header row whose headers best match the import type."""
    fields = FIELDS[import_type]
    required = {f.key for f in fields if f.required}
    best = None
    for s_idx, sheet in enumerate(sheets):
        for r_idx, row in enumerate(sheet.rows[:HEADER_SCAN_ROWS]):
            if is_blank_row(row):
                continue
            mapping, score = suggest_columns(row, fields)
            if not required <= set(mapping):
                score -= 100  # usable only if nothing better exists
            if best is None or score > best["score"]:
                best = {"sheet": sheet.name, "sheet_index": s_idx, "header_row": r_idx, "score": score,
                        "columns": mapping}
    if best is None or best["score"] <= 0:
        return None
    return best
