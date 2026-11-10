"""Home dashboard (Phase 11): what needs doing today, and how the business is going.

Every block is computed from the same services the detail pages use (so the numbers always match
those pages) and is only included when the user has the permission for it. Money only with finance
access; cost only with cost access.
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import ledger, livestream_report, logistics, pnl, replenishment, trends
from app.services.inventory_queries import can_view_cost


def _count(db: Session, sql: str, **p) -> int:
    return int(db.execute(text(sql), p).scalar_one() or 0)


def build(db: Session, permissions: set[str]) -> dict:
    can = permissions.__contains__
    money = can("finance.view") or can("finance.view_livestream")
    today = ledger.biz_date(datetime.now(UTC))
    monday = today - timedelta(days=today.weekday())
    out: dict = {"today": today, "week_start": monday, "todo": []}

    def todo(key: str, count: int, link: str, severity: str = "warn") -> None:
        if count:
            out["todo"].append({"key": key, "count": count, "link": link, "severity": severity})

    if can("orders.view"):
        todo("order_exceptions", _count(db, """SELECT count(*) FROM sales_order_lines
                                               WHERE exception_code IS NOT NULL AND exception_code <> 'unmapped_sku'"""),
             "/orders?tab=exceptions", "danger")
        todo("unmapped_products", _count(db, r"""
            SELECT count(DISTINCT regexp_replace(l.title_snapshot, '\s*#\d+\s*$', '')) FROM sales_order_lines l
              JOIN sales_orders o ON o.id = l.order_id
             WHERE l.sku_id IS NULL AND o.status IN ('pending_fulfillment', 'completed')"""), "/orders?tab=unmapped")
        b = logistics.outbound_backlog(db, limit=1)
        todo("late_outbound", b["late_orders"], "/logistics?tab=outbound", "danger")
    if can("inventory.view"):
        parcels = logistics.list_parcels(db, status="open")
        todo("overdue_parcels", sum(1 for p in parcels if p["overdue_days"] > 0), "/logistics", "danger")
        todo("stock_counts_to_approve", _count(db, "SELECT count(*) FROM stock_counts WHERE status = 'submitted'"),
             "/stock/counts")
        out["parcels_in_transit"] = len(parcels)
    if can("replenishment.view"):
        last = replenishment.run_detail(db)
        if last["run"]:
            red = sum(1 for s in last["suggestions"] if s["status_color"] == "red" and not s["confirmed_at"])
            todo("replenish_now", red, "/replenishment", "danger")
            out["replenishment_as_of"] = last["run"]["data_as_of"]
        else:
            todo("replenishment_never_run", 1, "/replenishment", "info")
    if can("integration.manage") or can("finance.view"):
        todo("whatnot_review", _count(db, """SELECT count(*) FROM platform_transactions
                                             WHERE status = 'needs_review' OR upstream_changed"""),
             "/settings/whatnot")
    if can("integration.manage"):
        last_sync = db.execute(text("""SELECT status, started_at, finished_at FROM sync_runs WHERE platform = 'whatnot'
                                       ORDER BY id DESC LIMIT 1""")).mappings().first()
        out["last_sync"] = dict(last_sync) if last_sync else None
        if not last_sync or last_sync["started_at"] < datetime.now(UTC) - timedelta(hours=6):
            todo("sync_stale", 1, "/settings/whatnot", "info")

    # ---- sales: this week vs last week, last 8 weeks ----
    if can("orders.view"):
        start8 = monday - timedelta(weeks=7)
        ov = trends.overview(db, start8, today, "week", "sku", with_money=money)
        cols = ov["columns"]
        out["sales_weeks"] = [{"label": c["label"], "from": c["from"], "sales": c["sales"], "units": c["units"],
                               "orders": c["orders"], "shows": c["shows"]} for c in cols]
        this, prev = cols[-1], (cols[-2] if len(cols) > 1 else None)
        # compare the same number of days of last week, not the whole week
        days = (today - monday).days
        if prev:
            pw = trends.overview(db, monday - timedelta(days=7), monday - timedelta(days=7 - days), "total",
                                 with_money=money)["columns"][0]
        out["this_week"] = {"sales": this["sales"], "units": this["units"], "orders": this["orders"],
                            "shows": this["shows"],
                            "last_week_same_days": ({"sales": pw["sales"], "units": pw["units"], "orders": pw["orders"]}
                                                    if prev else None)}
    # ---- profit this month (finance) ----
    if can("finance.view"):
        first = today.replace(day=1)
        rep = pnl.report(db, first, today, "total")
        t = rep["total"]
        out["month"] = {"from": first, "net_sales": t["net_sales"], "gross_profit": t["gross_profit"],
                        "gross_margin_pct": t["gross_margin_pct"], "opex": t["opex"], "net_profit": t["net_profit"],
                        "net_margin_pct": t["net_margin_pct"], "complete": rep["completeness"]["complete"]}
    # ---- recent shows ----
    if can("finance.view_livestream"):
        shows = livestream_report.list_shows(db, today - timedelta(days=14), today)[:5]
        out["recent_shows"] = [{k: s[k] for k in ("id", "title", "start_at", "paid_orders", "net_sales", "gross_profit",
                                                   "margin_pct", "complete", "stream_minutes")} for s in shows]
    # ---- stock ----
    if can("inventory.view"):
        pos = db.execute(text("""SELECT COALESCE(sum(us_available), 0) AS available, COALESCE(sum(in_transit), 0) AS transit,
                                        count(*) FILTER (WHERE us_available <= 0) AS out_of_stock
                                   FROM v_inventory_position p JOIN skus s ON s.id = p.sku_id
                                  WHERE s.status = 'active' AND NOT s.is_bundle""")).mappings().one()
        stock = {k: int(v) for k, v in pos.items()}
        if can_view_cost(permissions):
            stock["us_value"] = float(db.execute(text("""SELECT COALESCE(sum(value_base), 0) FROM v_inventory_valuation
                                                         WHERE location_code = 'US_MAIN'""")).scalar_one())
            stock["transit_value"] = float(db.execute(text("""SELECT COALESCE(sum(value_base), 0) FROM v_inventory_valuation
                                                              WHERE location_code = 'IN_TRANSIT'""")).scalar_one())
        out["stock"] = stock
    return out
