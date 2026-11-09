"""Per-show P&L (Phase 6).

Everything is read from the ledger (financial_entries), so the report always agrees with the books:

  sales (item revenue, from orders)            revenue           +
  coupons                                      sales_discount    -
  refunds (Whatnot financial report)           refund / refund_adjustment
  Whatnot commission (+ tax)                   platform_fee      -
  payment processing (+ tax)                   payment_processing_fee -
  seller-paid shipping, carrier adjustments    shipping_cost / shipping_adjustment
  cost of goods (FIFO lots, at shipping)       cogs / cogs_adjustment
  tips                                         other_income      +
  ------------------------------------------------------------------
  gross profit of the show = sum of the above   (before operating expenses: Phase 7)

A show's entries are found through their order (sales_orders.livestream_id) so that a show learned
after booking still collects them; entries with no order (tips, carrier adjustments) use their own
livestream_id. Nothing is estimated: when fees or cost of goods are not known yet, the report says
how much is missing instead of guessing.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError

ZERO = Decimal(0)
GROUPS = {
    "revenue": "sales", "sales_discount": "discounts", "refund": "refunds", "refund_adjustment": "refunds",
    "platform_fee": "commission", "payment_processing_fee": "processing", "shipping_cost": "shipping",
    "shipping_adjustment": "shipping", "cogs": "cogs", "cogs_adjustment": "cogs", "other_income": "other",
    "shipping_income": "other", "other_expense": "other", "operating_expense": "other",
}
LINES = ("sales", "discounts", "refunds", "commission", "processing", "shipping", "cogs", "other")
PAID = ("pending_fulfillment", "completed")

# entries -> show, through the order when there is one
_ENTRY_SHOW = """
    SELECT COALESCE(o.livestream_id, e.livestream_id) AS livestream_id, e.entry_type, e.amount_base,
           e.sales_order_id, e.order_line_id
      FROM financial_entries e LEFT JOIN sales_orders o ON o.id = e.sales_order_id
"""


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().business_timezone)


def _f(d: Decimal | None) -> float:
    return float(round(d or ZERO, 2))


def _summary(b: dict[str, Decimal]) -> dict:
    out = {k: _f(b.get(k)) for k in LINES}
    net_sales = (b.get("sales") or ZERO) + (b.get("discounts") or ZERO) + (b.get("refunds") or ZERO)
    fees = sum((b.get(k) or ZERO) for k in ("commission", "processing", "shipping"))
    profit = sum((b.get(k) or ZERO) for k in LINES)
    out.update(net_sales=_f(net_sales), platform_costs=_f(fees), gross_profit=_f(profit),
               margin_pct=(float(round(profit / net_sales * 100, 1)) if net_sales > 0 else None))
    return out


def _show_filter(livestream_id: int | None) -> tuple[str, dict]:
    if livestream_id is None:
        return "livestream_id IS NULL", {}
    return "livestream_id = :ls", {"ls": livestream_id}


def _orders_stats(db: Session, where: str, params: dict) -> dict[int | None, dict]:
    rows = db.execute(text(f"""
        WITH o AS (SELECT * FROM sales_orders WHERE platform = 'whatnot' AND status IN ('pending_fulfillment', 'completed')
                   AND {where})
        SELECT o.livestream_id,
               count(*) AS paid_orders,
               count(*) FILTER (WHERE o.item_subtotal = 0) AS giveaways,
               COALESCE(sum((SELECT sum(qty) FROM sales_order_lines l WHERE l.order_id = o.id)), 0) AS units,
               count(*) FILTER (WHERE NOT EXISTS (
                   SELECT 1 FROM platform_transactions t WHERE t.sales_order_id = o.id
                      AND t.transaction_type = 'ORDER_EARNINGS' AND t.status = 'booked')) AS orders_without_fees,
               COALESCE(sum((SELECT sum(qty) FROM sales_order_lines l WHERE l.order_id = o.id
                             AND l.stock_state <> 'shipped')), 0) AS units_without_cogs,
               COALESCE(sum((SELECT count(*) FROM sales_order_lines l WHERE l.order_id = o.id
                             AND l.exception_code IS NOT NULL)), 0) AS lines_with_issues,
               min(o.ordered_at) AS first_order_at, max(o.ordered_at) AS last_order_at
          FROM o GROUP BY o.livestream_id"""), params).mappings()
    return {r["livestream_id"]: dict(r) for r in rows}


def _metrics(db: Session, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    out: dict[int, dict] = defaultdict(dict)
    for r in db.execute(text("""SELECT livestream_id, metric_code, value FROM livestream_metrics
                                WHERE livestream_id = ANY(:ids) AND source = 'api'"""), {"ids": ids}):
        out[r.livestream_id][r.metric_code] = float(r.value)
    return out


def _coverage(o: dict) -> dict:
    return {"paid_orders": o.get("paid_orders", 0), "giveaways": o.get("giveaways", 0), "units": int(o.get("units", 0)),
            "orders_without_fees": o.get("orders_without_fees", 0),
            "units_without_cogs": int(o.get("units_without_cogs", 0)),
            "lines_with_issues": int(o.get("lines_with_issues", 0)),
            "complete": not (o.get("orders_without_fees") or o.get("units_without_cogs") or o.get("lines_with_issues"))}


def list_shows(db: Session, date_from: date | None = None, date_to: date | None = None) -> list[dict]:
    tz = _tz()
    date_to = date_to or datetime.now(tz).date()
    date_from = date_from or date_to - timedelta(days=30)
    start = datetime.combine(date_from, time.min, tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz)
    shows = db.execute(text("""
        SELECT l.id, l.external_id, l.title, l.status, l.analytics_status,
               COALESCE(l.started_at, l.scheduled_at, (SELECT min(ordered_at) FROM sales_orders o WHERE o.livestream_id = l.id))
                   AS start_at, l.ended_at
          FROM livestreams l
         WHERE l.platform = 'whatnot'
           AND COALESCE(l.started_at, l.scheduled_at, (SELECT min(ordered_at) FROM sales_orders o WHERE o.livestream_id = l.id))
               >= :s
           AND COALESCE(l.started_at, l.scheduled_at, (SELECT min(ordered_at) FROM sales_orders o WHERE o.livestream_id = l.id))
               < :e
           AND EXISTS (SELECT 1 FROM sales_orders o WHERE o.livestream_id = l.id)
         ORDER BY start_at DESC"""), {"s": start, "e": end}).mappings().all()
    ids = [s["id"] for s in shows]
    buckets: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    if ids:
        for r in db.execute(text(f"SELECT livestream_id, entry_type, sum(amount_base) AS amt FROM ({_ENTRY_SHOW}) x "
                                 "WHERE livestream_id = ANY(:ids) GROUP BY 1, 2"), {"ids": ids}):
            buckets[r.livestream_id][GROUPS.get(r.entry_type, "other")] += r.amt
    orders = _orders_stats(db, "livestream_id = ANY(:ids)", {"ids": ids}) if ids else {}
    metrics = _metrics(db, ids)
    out = []
    for s in shows:
        summ = _summary(buckets[s["id"]])
        m = metrics.get(s["id"], {})
        minutes = m.get("stream_minutes")
        out.append({**dict(s), **summ, **_coverage(orders.get(s["id"], {})), "stream_minutes": minutes,
                    "profit_per_hour": (round(summ["gross_profit"] / (minutes / 60), 2) if minutes else None),
                    "whatnot_sales": m.get("sales"), "whatnot_estimated_earnings": m.get("estimated_earnings")})
    return out


def show_detail(db: Session, livestream_id: int) -> dict:
    ls = db.execute(text("""SELECT id, external_id, title, status, analytics_status, analytics_fetched_at,
                                   started_at, scheduled_at, ended_at FROM livestreams WHERE id = :i"""),
                    {"i": livestream_id}).mappings().first()
    if ls is None:
        raise AppError("not_found", "Livestream not found", 404)
    where, params = _show_filter(livestream_id)
    entries = db.execute(text(f"SELECT * FROM ({_ENTRY_SHOW}) x WHERE {where}"), params).mappings().all()
    bucket: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in entries:
        bucket[GROUPS.get(e["entry_type"], "other")] += e["amount_base"]
    summary = _summary(bucket)
    orders = _orders_stats(db, where, params).get(livestream_id, {})
    metrics = _metrics(db, [livestream_id]).get(livestream_id, {})

    # ---- per SKU: line-level entries direct, order-level entries spread by the line's share ----
    lines = db.execute(text("""
        SELECT l.id, l.order_id, l.sku_id, l.qty, l.line_amount, l.title_snapshot, l.stock_state, l.exception_code,
               s.sku_code, s.name
          FROM sales_order_lines l JOIN sales_orders o ON o.id = l.order_id LEFT JOIN skus s ON s.id = l.sku_id
         WHERE o.livestream_id = :ls AND o.status IN ('pending_fulfillment', 'completed')"""),
                       {"ls": livestream_id}).mappings().all()
    by_order: dict[int, list] = defaultdict(list)
    for ln in lines:
        by_order[ln["order_id"]].append(ln)
    per_line: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    unallocated: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in entries:
        g = GROUPS.get(e["entry_type"], "other")
        if e["order_line_id"] and e["order_line_id"] in {ln["id"] for ln in by_order.get(e["sales_order_id"], [])}:
            per_line[e["order_line_id"]][g] += e["amount_base"]
        elif e["sales_order_id"] in by_order:
            ols = by_order[e["sales_order_id"]]
            total = sum(ln["line_amount"] for ln in ols)
            for ln in ols:
                share = (ln["line_amount"] / total) if total else Decimal(ln["qty"]) / sum(x["qty"] for x in ols)
                per_line[ln["id"]][g] += e["amount_base"] * share
        else:
            unallocated[g] += e["amount_base"]
    skus: dict = {}
    for ln in lines:
        key = ln["sku_id"] or f"unmapped:{ln['title_snapshot']}"
        row = skus.setdefault(key, {"sku_id": ln["sku_id"], "sku_code": ln["sku_code"],
                                    "name": ln["name"] or ln["title_snapshot"], "mapped": ln["sku_id"] is not None,
                                    "units": 0, "units_without_cogs": 0, "_b": defaultdict(lambda: ZERO)})
        row["units"] += ln["qty"]
        row["units_without_cogs"] += ln["qty"] if ln["stock_state"] != "shipped" else 0
        for g, v in per_line[ln["id"]].items():
            row["_b"][g] += v
    sku_rows = []
    for row in skus.values():
        b = row.pop("_b")
        s = _summary(b)
        sku_rows.append({**row, **{k: s[k] for k in ("sales", "net_sales", "platform_costs", "cogs", "gross_profit",
                                                     "margin_pct")}})
    sku_rows.sort(key=lambda r: -r["net_sales"])

    refunds = db.execute(text("""
        SELECT t.external_order_id, t.transaction_type, t.amount, t.completed_at, t.message
          FROM platform_transactions t LEFT JOIN sales_orders o ON o.id = t.sales_order_id
         WHERE COALESCE(o.livestream_id, t.livestream_id) = :ls
           AND t.transaction_type IN ('ORDER_REFUND', 'APPEALED_ORDER_REFUND')
         ORDER BY t.completed_at"""), {"ls": livestream_id}).mappings().all()
    review = db.execute(text("""
        SELECT t.external_id, t.transaction_type, t.external_order_id, t.amount, t.status, t.detail, t.upstream_changed
          FROM platform_transactions t LEFT JOIN sales_orders o ON o.id = t.sales_order_id
         WHERE COALESCE(o.livestream_id, t.livestream_id) = :ls
           AND (t.status <> 'booked' OR t.upstream_changed OR t.detail LIKE '金額不符%')
         ORDER BY t.completed_at LIMIT 200"""), {"ls": livestream_id}).mappings().all()
    missing_fees = db.execute(text("""
        SELECT o.id, o.external_order_id, o.item_subtotal, o.status, o.ordered_at
          FROM sales_orders o
         WHERE o.livestream_id = :ls AND o.status IN ('pending_fulfillment', 'completed')
           AND NOT EXISTS (SELECT 1 FROM platform_transactions t WHERE t.sales_order_id = o.id
                           AND t.transaction_type = 'ORDER_EARNINGS' AND t.status = 'booked')
         ORDER BY o.ordered_at LIMIT 100"""), {"ls": livestream_id}).mappings().all()

    minutes = metrics.get("stream_minutes")
    ours_vs_whatnot = None
    if "sales" in metrics or "estimated_earnings" in metrics:
        # Whatnot "estimated earnings" = sales - commission - processing - seller shipping (no cost of goods)
        ours_earnings = summary["net_sales"] + summary["commission"] + summary["processing"] + summary["shipping"]
        ours_vs_whatnot = {
            "whatnot_sales": metrics.get("sales"), "ours_sales": summary["sales"] + summary["discounts"],
            "whatnot_estimated_earnings": metrics.get("estimated_earnings"), "ours_earnings": round(ours_earnings, 2),
            "whatnot_orders": metrics.get("orders_count"), "ours_orders": orders.get("paid_orders", 0),
        }
    return {
        "livestream": dict(ls), "summary": summary, "coverage": _coverage(orders),
        "stream_minutes": minutes,
        "per_hour": ({"sales": round(summary["net_sales"] / (minutes / 60), 2),
                      "gross_profit": round(summary["gross_profit"] / (minutes / 60), 2)} if minutes else None),
        "whatnot_metrics": metrics, "comparison": ours_vs_whatnot, "skus": sku_rows,
        "unallocated": {k: _f(v) for k, v in unallocated.items() if v},
        "refunds": [dict(r) for r in refunds], "needs_review": [dict(r) for r in review],
        "orders_without_fees": [dict(r) for r in missing_fees],
    }


def financial_status(db: Session) -> dict:
    """Reconciliation overview for the integration page."""
    by = {r.status: {"count": r.n, "amount": _f(r.amt)} for r in db.execute(text(
        "SELECT status, count(*) AS n, sum(amount) AS amt FROM platform_transactions GROUP BY 1"))}
    runs = db.execute(text("""SELECT external_id, status, parameters, processed_at, stats, error, created_at
                              FROM platform_report_runs ORDER BY id DESC LIMIT 10""")).mappings().all()
    return {"transactions": by, "upstream_changed": db.scalar(text(
        "SELECT count(*) FROM platform_transactions WHERE upstream_changed")),
            "report_runs": [dict(r) for r in runs]}
