import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import make_sku, uid


def batch_with_line(db):
    sku = make_sku(db)
    b = db.execute(text("INSERT INTO purchase_batches (batch_no, purchase_date) VALUES (:n, '2026-11-01') RETURNING id"),
                   {"n": uid("PB-")}).scalar_one()
    line = db.execute(text("""
        INSERT INTO purchase_lines (batch_id, line_no, sku_id, qty, unit_price_orig, currency_orig, fx_rate_to_base,
                                    landed_total_base, landed_unit_cost_base)
        VALUES (:b, 1, :s, 10, 1500, 'JPY', 0.0066, 99.0, 9.9) RETURNING id"""), {"b": b, "s": sku}).scalar_one()
    return b, line, sku


def test_draft_batch_is_editable(db):
    _, line, _ = batch_with_line(db)
    db.execute(text("UPDATE purchase_lines SET qty = 12 WHERE id = :l"), {"l": line})


def test_confirmed_batch_cost_cannot_be_overwritten(db):
    b, line, sku = batch_with_line(db)
    db.execute(text("UPDATE purchase_batches SET status = 'confirmed', confirmed_at = now() WHERE id = :b"), {"b": b})
    stmts = [
        ("UPDATE purchase_lines SET unit_price_orig = 1 WHERE id = :l", {"l": line}),
        ("DELETE FROM purchase_lines WHERE id = :l", {"l": line}),
        ("""INSERT INTO purchase_lines (batch_id, line_no, sku_id, qty, unit_price_orig, currency_orig,
               fx_rate_to_base, landed_total_base, landed_unit_cost_base)
            VALUES (:b, 2, :s, 1, 1, 'JPY', 0.0066, 0.0066, 0.0066)""", {"b": b, "s": sku}),
        ("UPDATE purchase_batches SET status = 'draft' WHERE id = :b", {"b": b}),
        ("UPDATE purchase_batches SET purchase_date = '2026-01-01' WHERE id = :b", {"b": b}),
        ("DELETE FROM purchase_batches WHERE id = :b", {"b": b}),
    ]
    for sql, params in stmts:
        with pytest.raises(DBAPIError, match="ERP_BATCH_LOCKED"):
            with db.begin_nested():
                db.execute(text(sql), params)


def test_purchase_order_needs_approval_before_ordering(db):
    with pytest.raises(IntegrityError, match="ck_purchase_orders_approved_before_ordering"):
        with db.begin_nested():
            db.execute(text("INSERT INTO purchase_orders (po_no, status) VALUES (:n, 'ordered')"), {"n": uid("PO-")})
    db.execute(text("INSERT INTO purchase_orders (po_no) VALUES (:n)"), {"n": uid("PO-")})  # draft is fine


def test_override_requires_reason(db):
    sku = make_sku(db)
    run = db.execute(text("INSERT INTO replenishment_runs (data_as_of, params_snapshot) VALUES ('2026-11-08', '{}') RETURNING id")).scalar_one()
    base = """INSERT INTO replenishment_suggestions (run_id, sku_id, v7, v14, v30, v_blended, sigma_daily, horizon_days,
              forecast_demand, safety_stock, us_available, in_transit_in_time, in_transit_late, jp_available,
              suggested_ship_qty, suggested_purchase_qty, status_color, override_ship_qty, override_reason)
              VALUES (:r, :s, 1, 1, 1, 1, 0.5, 16, 16, 2, 5, 0, 0, 20, 13, 0, 'orange', 20, :reason)"""
    with pytest.raises(IntegrityError, match="ck_replenishment_suggestions_override_needs_reason"):
        with db.begin_nested():
            db.execute(text(base), {"r": run, "s": sku, "reason": None})
    db.execute(text(base), {"r": run, "s": sku, "reason": "special show next week"})
