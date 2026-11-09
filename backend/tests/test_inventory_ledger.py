import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import loc, make_lot, make_sku, move, position, uid


def stocked_sku(db, qty=10):
    sku = make_sku(db)
    lot = make_lot(db, sku, qty=qty)
    move(db, sku, lot, "opening_balance", "ADJUSTMENT", "JP_MAIN", qty)
    return sku, lot


def test_movement_updates_balances_automatically(db):
    sku, _ = stocked_sku(db, 10)
    assert position(db, sku)["jp_on_hand"] == 10


def test_full_jp_to_us_flow_and_in_transit_is_not_sellable(db):
    sku, lot = stocked_sku(db, 10)
    move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 6)
    p = position(db, sku)
    assert (p["jp_on_hand"], p["in_transit"], p["us_on_hand"], p["us_available"]) == (4, 6, 0, 0)

    move(db, sku, lot, "transfer_receive", "IN_TRANSIT", "US_MAIN", 5)
    move(db, sku, lot, "transfer_loss", "IN_TRANSIT", "LOSS", 1)
    move(db, sku, lot, "sale", "US_MAIN", "CUSTOMER", 2)
    p = position(db, sku)
    assert (p["jp_on_hand"], p["in_transit"], p["us_on_hand"], p["us_available"]) == (4, 0, 3, 3)


def test_cannot_sell_from_in_transit_or_japan(db):
    sku, lot = stocked_sku(db, 10)
    move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 5)
    for frm in ("IN_TRANSIT", "JP_MAIN"):
        with pytest.raises(DBAPIError, match="ERP_MOVEMENT_DIRECTION"):
            with db.begin_nested():
                move(db, sku, lot, "sale", frm, "CUSTOMER", 1)


def test_cannot_overdraw_stock(db):
    sku, lot = stocked_sku(db, 3)
    with pytest.raises(IntegrityError, match="ck_inventory_balances_qty_nonneg"):
        with db.begin_nested():
            move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 4)
    assert position(db, sku)["jp_on_hand"] == 3  # unchanged, whole movement rolled back


def test_cannot_move_stock_that_was_never_there(db):
    sku = make_sku(db)
    lot = make_lot(db, sku)
    with pytest.raises(DBAPIError, match="ERP_INSUFFICIENT_STOCK"):
        with db.begin_nested():
            move(db, sku, lot, "sale", "US_MAIN", "CUSTOMER", 1)


def test_same_idempotency_key_cannot_deduct_twice(db):
    sku, lot = stocked_sku(db, 10)
    move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 2, key="ship:line-1:lot")
    with pytest.raises(IntegrityError, match="uq_inventory_movements_idempotency_key"):
        with db.begin_nested():
            move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 2, key="ship:line-1:lot")
    assert position(db, sku)["in_transit"] == 2


def test_movements_are_append_only(db):
    sku, lot = stocked_sku(db, 10)
    mid = db.execute(text("SELECT id FROM inventory_movements WHERE sku_id = :s"), {"s": sku}).scalar_one()
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"):
        with db.begin_nested():
            db.execute(text("UPDATE inventory_movements SET qty = 999 WHERE id = :i"), {"i": mid})
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"):
        with db.begin_nested():
            db.execute(text("DELETE FROM inventory_movements WHERE id = :i"), {"i": mid})
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"):
        with db.begin_nested():
            db.execute(text("TRUNCATE inventory_movements CASCADE"))


def test_lot_original_cost_is_immutable(db):
    sku = make_sku(db)
    lot = make_lot(db, sku)
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"):
        with db.begin_nested():
            db.execute(text("UPDATE inventory_lots SET original_unit_cost_base = 1 WHERE id = :i"), {"i": lot})


def test_balances_cannot_be_written_directly(db):
    sku, lot = stocked_sku(db, 10)
    with pytest.raises(DBAPIError, match="ERP_BALANCE_DIRECT_WRITE"):
        with db.begin_nested():
            db.execute(text("UPDATE inventory_balances SET qty = 500 WHERE sku_id = :s"), {"s": sku})
    with pytest.raises(DBAPIError, match="ERP_BALANCE_DIRECT_WRITE"):
        with db.begin_nested():
            db.execute(
                text("INSERT INTO inventory_balances (sku_id, location_id, lot_id, qty) VALUES (:s, :l, :lot, 5)"),
                {"s": sku, "l": loc(db, "US_MAIN"), "lot": lot},
            )


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_quantity_rejected(db, qty):
    sku, lot = stocked_sku(db, 10)
    with pytest.raises(IntegrityError, match="ck_inventory_movements_qty_positive"):
        with db.begin_nested():
            move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", qty)


def test_lot_must_belong_to_sku(db):
    sku_a, lot_a = stocked_sku(db, 10)
    sku_b = make_sku(db)
    with pytest.raises(DBAPIError, match="ERP_LOT_SKU_MISMATCH"):
        with db.begin_nested():
            move(db, sku_b, lot_a, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 1)


def test_reservations_reduce_us_available_only(db):
    sku, lot = stocked_sku(db, 10)
    move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 8)
    move(db, sku, lot, "transfer_receive", "IN_TRANSIT", "US_MAIN", 5)
    oid = db.execute(text("""INSERT INTO sales_orders (external_order_id, status, ordered_at, item_subtotal, total, source)
                             VALUES (:e, 'paid', now(), 20, 20, 'manual') RETURNING id"""), {"e": uid()}).scalar_one()
    line = db.execute(text("""INSERT INTO sales_order_lines (order_id, external_line_id, sku_id, qty, unit_price,
                                line_amount, mapping_status)
                              VALUES (:o, 'l1', :s, 2, 10, 20, 'mapped') RETURNING id"""),
                      {"o": oid, "s": sku}).scalar_one()
    db.execute(text("INSERT INTO inventory_reservations (order_line_id, sku_id, location_id, qty) VALUES (:l, :s, :loc, 2)"),
               {"l": line, "s": sku, "loc": loc(db, "US_MAIN")})
    p = position(db, sku)
    assert (p["us_on_hand"], p["us_reserved"], p["us_available"], p["in_transit"]) == (5, 2, 3, 3)


def test_ledger_and_balances_reconcile(db):
    sku, lot = stocked_sku(db, 10)
    move(db, sku, lot, "transfer_ship", "JP_MAIN", "IN_TRANSIT", 4)
    move(db, sku, lot, "transfer_receive", "IN_TRANSIT", "US_MAIN", 4)
    move(db, sku, lot, "sale", "US_MAIN", "CUSTOMER", 1)
    diffs = db.execute(text("SELECT * FROM v_inventory_reconciliation WHERE sku_id = :s"), {"s": sku}).all()
    assert diffs == []


def test_sku_code_unique_case_insensitive(db):
    make_sku(db, code="ABC-001")
    with pytest.raises(IntegrityError, match="uq_skus_sku_code_upper"):
        with db.begin_nested():
            make_sku(db, code="abc-001")


class TestBundles:
    def test_bundle_holds_no_stock(self, db):
        bundle = make_sku(db, is_bundle=True)
        with pytest.raises(DBAPIError, match="ERP_BUNDLE_NO_STOCK"):
            with db.begin_nested():
                make_lot(db, bundle)

    def test_bundle_components(self, db):
        bundle = make_sku(db, is_bundle=True)
        a, b = make_sku(db), make_sku(db)
        db.execute(text("INSERT INTO sku_bundle_components VALUES (:b, :c, 2)"), {"b": bundle, "c": a})
        db.execute(text("INSERT INTO sku_bundle_components VALUES (:b, :c, 1)"), {"b": bundle, "c": b})
        n = db.execute(text("SELECT count(*) FROM sku_bundle_components WHERE bundle_sku_id = :b"), {"b": bundle})
        assert n.scalar_one() == 2

    def test_component_parent_must_be_bundle_and_no_nesting(self, db):
        plain, comp = make_sku(db), make_sku(db)
        with pytest.raises(DBAPIError, match="ERP_NOT_A_BUNDLE"):
            with db.begin_nested():
                db.execute(text("INSERT INTO sku_bundle_components VALUES (:b, :c, 1)"), {"b": plain, "c": comp})
        outer, inner = make_sku(db, is_bundle=True), make_sku(db, is_bundle=True)
        with pytest.raises(DBAPIError, match="ERP_NESTED_BUNDLE"):
            with db.begin_nested():
                db.execute(text("INSERT INTO sku_bundle_components VALUES (:b, :c, 1)"), {"b": outer, "c": inner})
