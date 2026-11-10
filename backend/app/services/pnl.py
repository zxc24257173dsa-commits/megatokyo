"""Profit & loss for a date range, by week (Monday start) or month, in the business timezone (Phase 7).

Read only from the ledger (financial_entries) by business date (entry_date):

  sales                    revenue
  discounts                sales_discount                       \\
  refunds                  refund, refund_adjustment             > net sales
  ---------------------------------------------------------------/
  Whatnot commission       platform_fee                         \\
  payment processing       payment_processing_fee                > platform costs
  seller-paid shipping     shipping_cost, shipping_adjustment   /
  cost of goods            cogs, cogs_adjustment
  inventory loss / gain    inventory_adjustment (damage, count differences, lost in transit)
  = gross profit
  other income / expense   other_income, shipping_income, other_expense
  operating expenses       operating_expense (by category; voids are reversal entries)
  = net profit

Revenue is booked on the order date and cost of goods on the shipping date (FIFO lots), so an
order sold at the end of a period and shipped in the next shows its cost in the next period; the
`completeness` block says how many units of the period's orders are still unshipped.
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import AppError

ZERO = Decimal(0)
GROUP = {
    "revenue": "sales", "sales_discount": "discounts", "refund": "refunds", "refund_adjustment": "refunds",
    "platform_fee": "commission", "payment_processing_fee": "processing", "shipping_cost": "shipping",
    "shipping_adjustment": "shipping", "cogs": "cogs", "cogs_adjustment": "cogs", "other_income": "other",
    "shipping_income": "other", "other_expense": "other", "operating_expense": "opex",
    "inventory_adjustment": "shrinkage",
}
MAX_BUCKETS = 120


def _f(d: Decimal | None) -> float:
    return float(round(d or ZERO, 2))


def buckets(start: date, end: date, granularity: str) -> list[tuple[date, date, str]]:
    """[(first day, last day, label)] covering start..end (inclusive), clipped to the range."""
    out = []
    if granularity == "month":
        cur = start.replace(day=1)
        while cur <= end:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            out.append((max(cur, start), min(nxt - timedelta(days=1), end), cur.strftime("%Y-%m")))
            cur = nxt
    elif granularity == "week":
        cur = start - timedelta(days=start.weekday())  # Monday
        while cur <= end:
            last = cur + timedelta(days=6)
            out.append((max(cur, start), min(last, end), f"{cur:%m/%d}–{last:%m/%d}"))
            cur = last + timedelta(days=1)
    elif granularity == "total":
        out.append((start, end, f"{start:%Y-%m-%d}–{end:%Y-%m-%d}"))
    else:
        raise AppError("invalid_granularity", "granularity must be week, month or total", 422)
    if len(out) > MAX_BUCKETS:
        raise AppError("range_too_long", "Choose a shorter range or a coarser grouping", 422)
    return out


def _lines(b: dict[str, Decimal], opex_by_cat: dict[str, Decimal]) -> dict:
    g = lambda k: b.get(k) or ZERO  # noqa: E731
    net_sales = g("sales") + g("discounts") + g("refunds")
    platform = g("commission") + g("processing") + g("shipping")
    gross = net_sales + platform + g("cogs") + g("shrinkage")
    net = gross + g("other") + g("opex")
    pct = lambda v: float(round(v / net_sales * 100, 1)) if net_sales > 0 else None  # noqa: E731
    return {
        "sales": _f(g("sales")), "discounts": _f(g("discounts")), "refunds": _f(g("refunds")), "net_sales": _f(net_sales),
        "commission": _f(g("commission")), "processing": _f(g("processing")), "shipping": _f(g("shipping")),
        "platform_costs": _f(platform), "cogs": _f(g("cogs")), "shrinkage": _f(g("shrinkage")), "gross_profit": _f(gross), "gross_margin_pct": pct(gross),
        "other": _f(g("other")), "opex": _f(g("opex")), "opex_by_category": {k: _f(v) for k, v in opex_by_cat.items()},
        "net_profit": _f(net), "net_margin_pct": pct(net),
    }


def report(db: Session, start: date, end: date, granularity: str = "month") -> dict:
    if end < start:
        raise AppError("invalid_range", "End date is before start date", 422)
    cols = buckets(start, end, granularity)
    rows = db.execute(text("""
        SELECT e.entry_date, e.entry_type, COALESCE(x.category, '') AS category, sum(e.amount_base) AS amt
          FROM financial_entries e
          LEFT JOIN operating_expenses x ON x.financial_entry_id = e.id OR x.void_entry_id = e.id
         WHERE e.entry_date BETWEEN :s AND :e
         GROUP BY 1, 2, 3"""), {"s": start, "e": end}).all()
    per: list[dict[str, Decimal]] = [defaultdict(lambda: ZERO) for _ in cols]
    per_cat: list[dict[str, Decimal]] = [defaultdict(lambda: ZERO) for _ in cols]
    tot: dict[str, Decimal] = defaultdict(lambda: ZERO)
    tot_cat: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        i = next(i for i, (a, b, _) in enumerate(cols) if a <= r.entry_date <= b)
        grp = GROUP.get(r.entry_type, "other")
        per[i][grp] += r.amt
        tot[grp] += r.amt
        if grp == "opex":
            per_cat[i][r.category or "other"] += r.amt
            tot_cat[r.category or "other"] += r.amt
    cats = db.execute(text("SELECT code, name FROM expense_categories ORDER BY sort_order, code")).all()
    periods = dict(db.execute(text("SELECT period, status FROM accounting_periods WHERE period BETWEEN :a AND :b"),
                              {"a": start.strftime("%Y-%m"), "b": end.strftime("%Y-%m")}).all())
    return {
        "from": start, "to": end, "granularity": granularity,
        "columns": [{"from": a, "to": b, "label": lab, **_lines(per[i], per_cat[i]),
                     "period_status": periods.get(lab, "open") if granularity == "month" else None}
                    for i, (a, b, lab) in enumerate(cols)],
        "total": _lines(tot, tot_cat),
        "expense_categories": [{"code": c.code, "name": c.name} for c in cats],
        "completeness": completeness(db, start, end),
    }


def _tz() -> str:
    from app.core.config import get_settings
    return get_settings().business_timezone


def completeness(db: Session, start: date, end: date) -> dict:
    """What is still missing for the period's own orders (nothing is estimated)."""
    r = db.execute(text("""
        WITH o AS (
            SELECT o.* FROM sales_orders o
             WHERE o.platform = 'whatnot' AND o.status IN ('pending_fulfillment', 'completed')
               AND (o.ordered_at AT TIME ZONE :tz)::date BETWEEN :s AND :e)
        SELECT count(*) AS paid_orders,
               count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM platform_transactions t WHERE t.sales_order_id = o.id
                                AND t.transaction_type = 'ORDER_EARNINGS' AND t.status = 'booked')) AS orders_without_fees,
               COALESCE(sum((SELECT sum(qty) FROM sales_order_lines l WHERE l.order_id = o.id
                             AND l.stock_state <> 'shipped')), 0) AS units_without_cogs,
               COALESCE(sum((SELECT count(*) FROM sales_order_lines l WHERE l.order_id = o.id
                             AND l.exception_code IS NOT NULL)), 0) AS lines_with_issues
          FROM o"""), {"s": start, "e": end, "tz": _tz()}).mappings().one()
    review = db.scalar(text("""SELECT count(*) FROM platform_transactions
                               WHERE (status = 'needs_review' OR upstream_changed)
                                 AND (completed_at AT TIME ZONE :tz)::date BETWEEN :s AND :e"""),
                       {"s": start, "e": end, "tz": _tz()})
    fin_until = db.scalar(text("""SELECT max((parameters->>'end_date')::timestamptz) FROM platform_report_runs
                                  WHERE report_type = 'seller_financial_report' AND processed_at IS NOT NULL
                                    AND error IS NULL"""))
    out = {k: int(v) for k, v in r.items()}
    out.update(transactions_to_review=int(review or 0), financial_report_until=fin_until)
    out["complete"] = not (out["orders_without_fees"] or out["units_without_cogs"] or out["lines_with_issues"]
                           or out["transactions_to_review"])
    return out


def entries_csv(db: Session, start: date, end: date):
    """Every ledger line in the range (for the accountant). Yields CSV text rows."""
    import csv
    import io
    head = ["entry_date", "entry_type", "amount_usd", "amount_orig", "currency_orig", "fx_rate_usd",
            "whatnot_order_id", "livestream", "sku", "expense_category", "source", "source_ref", "reversal_of_id",
            "memo", "entry_id"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    yield "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 (Chinese) correctly
    q = db.execute(text("""
        SELECT e.entry_date, e.entry_type, e.amount_base, e.amount_orig, e.currency_orig, e.fx_rate,
               o.external_order_id, l.title, s.sku_code, x.category, e.source_type, e.source_ref, e.reversal_of_id,
               e.memo, e.id
          FROM financial_entries e
          LEFT JOIN sales_orders o ON o.id = e.sales_order_id
          LEFT JOIN livestreams l ON l.id = COALESCE(o.livestream_id, e.livestream_id)
          LEFT JOIN skus s ON s.id = e.sku_id
          LEFT JOIN operating_expenses x ON x.financial_entry_id = e.id OR x.void_entry_id = e.id
         WHERE e.entry_date BETWEEN :s AND :e ORDER BY e.entry_date, e.id"""),
                   {"s": start, "e": end}).yield_per(2000)
    for row in q:
        buf.seek(0)
        buf.truncate()
        w.writerow(["" if v is None else _safe_cell(v) for v in row])
        yield buf.getvalue()


def _safe_cell(v):
    """Neutralise spreadsheet formulas in text cells (CSV injection)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v
