"""Sales trends (Phase 8). Read-only, computed from orders and stock movements as they are now.

Two different "how much sold" numbers, on purpose:

  sold units      what buyers bought, as listed: paid Whatnot order lines by payment (order) date.
                  A bundle counts as the bundle; an opened card counts as the card.
  stock demand    what that took out of stock, per stocked SKU and day: non-bundle lines + bundle
                  lines expanded into their components + boxes opened (box SKU). Giveaways included
                  (they leave the warehouse too). This is the series replenishment uses (Phase 10).

Only paid orders (pending_fulfillment, completed) count; unpaid, cancelled and failed never do.
Lines not mapped to a SKU yet are grouped by listing title without the auction "#N".
Business dates are in the business timezone (America/New_York); weeks start on Monday.
"""
import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.services import ledger
from app.services.pnl import buckets as pnl_buckets

TOP_N = 12
TITLE_SQL = r"regexp_replace(l.title_snapshot, '\s*#\d+\s*$', '')"


def _tz() -> str:
    return get_settings().business_timezone


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    tz = ZoneInfo(_tz())
    return datetime.combine(start, time.min, tz), datetime.combine(end + timedelta(days=1), time.min, tz)


def buckets(start: date, end: date, granularity: str) -> list[tuple[date, date, str]]:
    if granularity == "day":
        if (end - start).days > 120:
            raise AppError("range_too_long", "Daily view covers at most 120 days", 422)
        return [(start + timedelta(days=i), start + timedelta(days=i), f"{start + timedelta(days=i):%m/%d}")
                for i in range((end - start).days + 1)]
    return pnl_buckets(start, end, granularity)


def _sold_rows(db: Session, start: date, end: date):
    s, e = _bounds(start, end)
    return db.execute(text(f"""
        SELECT (o.paid_at AT TIME ZONE :tz)::date AS d, l.sku_id, sk.sku_code, sk.name AS sku_name,
               p.id AS product_id, p.name AS product_name, c.id AS category_id, c.name AS category_name,
               CASE WHEN l.sku_id IS NULL THEN {TITLE_SQL} END AS title,
               sum(l.qty) AS units, sum(l.qty) FILTER (WHERE o.item_subtotal = 0) AS giveaway_units,
               sum(l.line_amount) AS sales, count(DISTINCT o.id) AS orders
          FROM sales_order_lines l
          JOIN sales_orders o ON o.id = l.order_id
          LEFT JOIN skus sk ON sk.id = l.sku_id
          LEFT JOIN products p ON p.id = sk.product_id
          LEFT JOIN categories c ON c.id = p.category_id
         WHERE o.platform = 'whatnot' AND o.status IN ('pending_fulfillment', 'completed')
           AND o.paid_at >= :s AND o.paid_at < :e
         GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9"""), {"tz": _tz(), "s": s, "e": e}).mappings().all()


def _key(r, group: str) -> tuple[str, str, int | None]:
    """(key, label, sku_id) for the chosen grouping."""
    if r["sku_id"] is None:
        return f"t:{r['title']}", r["title"] or "（未命名商品）", None
    if group == "product":
        return f"p:{r['product_id']}", r["product_name"], None
    if group == "category":
        return f"c:{r['category_id']}", r["category_name"] or "（未分類）", None
    return f"s:{r['sku_id']}", f"{r['sku_code']} {r['sku_name']}", r["sku_id"]


def overview(db: Session, start: date, end: date, granularity: str = "week", group: str = "sku",
             with_money: bool = True) -> dict:
    if end < start:
        raise AppError("invalid_range", "End date is before start date", 422)
    if group not in ("sku", "product", "category"):
        raise AppError("invalid_group", "group must be sku, product or category", 422)
    cols = buckets(start, end, granularity)
    idx = {}
    for i, (a, b, _) in enumerate(cols):
        d = a
        while d <= b:
            idx[d] = i
            d += timedelta(days=1)
    n = len(cols)
    tot = {"units": [0] * n, "giveaway_units": [0] * n, "sales": [0.0] * n}
    items: dict[str, dict] = {}
    for r in _sold_rows(db, start, end):
        i = idx[r["d"]]
        tot["units"][i] += int(r["units"])
        tot["giveaway_units"][i] += int(r["giveaway_units"] or 0)
        tot["sales"][i] += float(r["sales"])
        key, label, sku_id = _key(r, group)
        it = items.setdefault(key, {"key": key, "label": label, "sku_id": sku_id, "mapped": r["sku_id"] is not None,
                                    "units": [0] * n, "sales": [0.0] * n})
        it["units"][i] += int(r["units"])
        it["sales"][i] += float(r["sales"])

    # orders and shows per bucket (an order with several lines counts once)
    s, e = _bounds(start, end)
    per = db.execute(text("""
        SELECT (paid_at AT TIME ZONE :tz)::date AS d, count(*) AS orders
          FROM sales_orders WHERE platform = 'whatnot' AND status IN ('pending_fulfillment', 'completed')
           AND paid_at >= :s AND paid_at < :e GROUP BY 1"""), {"tz": _tz(), "s": s, "e": e}).all()
    orders = [0] * n
    for r in per:
        orders[idx[r.d]] += r.orders
    shows = [0] * n
    for i, (a, b, _) in enumerate(cols):
        sa, sb = _bounds(a, b)
        shows[i] = db.scalar(text("""SELECT count(DISTINCT livestream_id) FROM sales_orders
                                     WHERE platform = 'whatnot' AND status IN ('pending_fulfillment', 'completed')
                                       AND livestream_id IS NOT NULL AND paid_at >= :s AND paid_at < :e"""),
                             {"s": sa, "e": sb}) or 0

    ranked = sorted(items.values(), key=lambda x: (-sum(x["sales"]), -sum(x["units"])))
    top, rest = ranked[:TOP_N], ranked[TOP_N:]
    if rest:
        top.append({"key": "other", "label": f"其他 {len(rest)} 項", "sku_id": None, "mapped": True,
                    "units": [sum(x["units"][i] for x in rest) for i in range(n)],
                    "sales": [round(sum(x["sales"][i] for x in rest), 2) for i in range(n)]})
    for it in top:
        it["total_units"] = sum(it["units"])
        it["total_sales"] = round(sum(it["sales"]), 2)
        it["sales"] = [round(v, 2) for v in it["sales"]]
        if not with_money:
            it["sales"], it["total_sales"] = None, None
    return {
        "from": start, "to": end, "granularity": granularity, "group": group,
        "columns": [{"from": a, "to": b, "label": lab, "units": tot["units"][i],
                     "giveaway_units": tot["giveaway_units"][i], "orders": orders[i], "shows": shows[i],
                     "sales": round(tot["sales"][i], 2) if with_money else None,
                     "sales_per_show": (round(tot["sales"][i] / shows[i], 2) if with_money and shows[i] else None)}
                    for i, (a, b, lab) in enumerate(cols)],
        "items": top,
        "item_count": len(ranked),
        "unmapped_items": sum(1 for x in ranked if not x["mapped"]),
    }


# ---------- stock demand (per stocked SKU and day) ----------

def _demand_sql(where_sku: str = "") -> str:
    return f"""
    WITH paid AS (
        SELECT l.sku_id, l.qty, (o.paid_at AT TIME ZONE :tz)::date AS d
          FROM sales_order_lines l JOIN sales_orders o ON o.id = l.order_id
         WHERE o.platform = 'whatnot' AND o.status IN ('pending_fulfillment', 'completed') AND l.sku_id IS NOT NULL
           AND o.paid_at >= :s AND o.paid_at < :e
    ), d AS (
        SELECT p.sku_id, p.d, p.qty AS units FROM paid p JOIN skus k ON k.id = p.sku_id WHERE NOT k.is_bundle
        UNION ALL
        SELECT bc.component_sku_id, p.d, p.qty * bc.qty FROM paid p
          JOIN sku_bundle_components bc ON bc.bundle_sku_id = p.sku_id
        UNION ALL
        SELECT b.box_sku_id, (b.created_at AT TIME ZONE :tz)::date, b.qty FROM box_openings b
         WHERE b.created_at >= :s AND b.created_at < :e
    )
    SELECT sku_id, d, sum(units) AS units FROM d {where_sku} GROUP BY 1, 2"""


def daily_demand(db: Session, start: date, end: date, sku_id: int | None = None) -> dict[int, dict[date, int]]:
    s, e = _bounds(start, end)
    rows = db.execute(text(_demand_sql("WHERE sku_id = :sku" if sku_id else "")),
                      {"tz": _tz(), "s": s, "e": e, "sku": sku_id}).all()
    out: dict[int, dict[date, int]] = defaultdict(dict)
    for r in rows:
        out[r.sku_id][r.d] = int(r.units)
    return out


def classify(recent: int, prev: int) -> tuple[str, float | None]:
    if recent == 0 and prev == 0:
        return "none", None
    if prev == 0:
        return "new", None
    change = (recent - prev) / prev * 100
    if recent == 0:
        return "stopped", -100.0
    if recent + prev < 6:
        return "low", round(change, 1)
    if change >= 25:
        return "up", round(change, 1)
    if change <= -25:
        return "down", round(change, 1)
    return "flat", round(change, 1)


def movers(db: Session, weeks: int = 4, spark_weeks: int = 12, with_money: bool = True) -> dict:
    """Every active SKU: sold in the last `weeks` weeks vs the `weeks` before, stock demand per week,
    US sellable stock and how many weeks it lasts. In-transit stock is shown but never counted as sellable."""
    if not 1 <= weeks <= 26:
        raise AppError("invalid_window", "weeks must be between 1 and 26", 422)
    today = ledger.biz_date(datetime.now(UTC))
    recent_from = today - timedelta(days=7 * weeks - 1)
    prev_from = recent_from - timedelta(days=7 * weeks)
    spark_from = today - timedelta(days=7 * spark_weeks - 1)
    first = min(prev_from, spark_from)

    sold: dict[int, dict] = defaultdict(lambda: {"recent": 0, "prev": 0, "sales_recent": 0.0, "last_sale": None})
    for r in _sold_rows(db, first, today):
        if r["sku_id"] is None:
            continue
        x = sold[r["sku_id"]]
        if r["d"] >= recent_from:
            x["recent"] += int(r["units"])
            x["sales_recent"] += float(r["sales"])
        elif r["d"] >= prev_from:
            x["prev"] += int(r["units"])
        x["last_sale"] = max(x["last_sale"] or r["d"], r["d"])
    demand = daily_demand(db, first, today)

    skus = db.execute(text("""
        SELECT s.id, s.sku_code, s.name, s.is_bundle, s.status, p.name AS product_name,
               COALESCE(v.us_available, 0) AS us_available, COALESCE(v.in_transit, 0) AS in_transit,
               COALESCE(v.us_reserved, 0) AS us_reserved
          FROM skus s JOIN products p ON p.id = s.product_id
          LEFT JOIN v_inventory_position v ON v.sku_id = s.id
         WHERE s.status = 'active' OR s.id = ANY(:sold)"""), {"sold": list(sold) or [0]}).mappings().all()
    rows = []
    for s in skus:
        x = sold.get(s["id"], {"recent": 0, "prev": 0, "sales_recent": 0.0, "last_sale": None})
        trend, change = classify(x["recent"], x["prev"])
        dd = demand.get(s["id"], {})
        d_recent = sum(v for d, v in dd.items() if d >= recent_from)
        weekly = d_recent / weeks
        spark = [sum(dd.get(spark_from + timedelta(days=7 * w + i), 0) for i in range(7)) for w in range(spark_weeks)]
        rows.append({
            "sku_id": s["id"], "sku_code": s["sku_code"], "name": s["name"], "product_name": s["product_name"],
            "is_bundle": s["is_bundle"], "sold_recent": x["recent"], "sold_prev": x["prev"], "change_pct": change,
            "trend": trend, "sales_recent": round(x["sales_recent"], 2) if with_money else None,
            "last_sale": x["last_sale"], "demand_recent": d_recent, "weekly_demand": round(weekly, 2),
            "us_available": int(s["us_available"]), "us_reserved": int(s["us_reserved"]),
            "in_transit": int(s["in_transit"]),
            # bundles have no stock of their own: their demand lands on the components
            "weeks_of_cover": (None if s["is_bundle"] or weekly == 0 else round(int(s["us_available"]) / weekly, 1)),
            "weekly_spark": spark,
        })
    order = {"up": 0, "new": 1, "down": 2, "stopped": 3, "flat": 4, "low": 5, "none": 6}
    rows.sort(key=lambda r: (order[r["trend"]], -r["sold_recent"]))
    unm = db.execute(text(f"""
        SELECT count(DISTINCT {TITLE_SQL}) AS products, COALESCE(sum(l.qty), 0) AS units
          FROM sales_order_lines l JOIN sales_orders o ON o.id = l.order_id
         WHERE l.sku_id IS NULL AND o.status IN ('pending_fulfillment', 'completed')
           AND o.paid_at >= :s"""), {"s": _bounds(recent_from, today)[0]}).one()
    return {"weeks": weeks, "recent_from": recent_from, "prev_from": prev_from, "to": today,
            "spark_from": spark_from, "spark_weeks": spark_weeks, "skus": rows,
            "unmapped": {"products": unm.products, "units": int(unm.units)}}


def sku_demand(db: Session, sku_id: int, days: int = 90) -> dict:
    """Daily stock demand of one SKU, with the numbers replenishment will use (mean, std per day)."""
    if not 7 <= days <= 365:
        raise AppError("invalid_window", "days must be between 7 and 365", 422)
    sku = db.execute(text("SELECT id, sku_code, name, is_bundle, created_at FROM skus WHERE id = :i"),
                     {"i": sku_id}).mappings().first()
    if sku is None:
        raise AppError("not_found", "SKU not found", 404)
    today = ledger.biz_date(datetime.now(UTC))
    start = today - timedelta(days=days - 1)
    dd = daily_demand(db, start, today, sku_id).get(sku_id, {})
    series = [{"date": start + timedelta(days=i), "units": dd.get(start + timedelta(days=i), 0)} for i in range(days)]
    vals = [p["units"] for p in series]
    mean = sum(vals) / days
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (days - 1)) if days > 1 else 0.0
    return {"sku_id": sku_id, "sku_code": sku["sku_code"], "name": sku["name"], "is_bundle": sku["is_bundle"],
            "from": start, "to": today, "days": days, "series": series, "total": sum(vals),
            "mean_per_day": round(mean, 3), "std_per_day": round(std, 3),
            "days_with_demand": sum(1 for v in vals if v > 0)}
