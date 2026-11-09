"""Two warehouse users / webhook workers try to take the last unit at the same moment.
Exactly one must succeed; stock must never go negative. Uses real, separately committed
connections (data is cleaned when the session fixture re-migrates the test DB)."""
import threading

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import make_lot, make_sku, move


def test_last_unit_cannot_be_sold_twice_concurrently(engine):
    with Session(engine) as s:
        sku = make_sku(s)
        lot = make_lot(s, sku, qty=1)
        move(s, sku, lot, "opening_balance", "ADJUSTMENT", "US_MAIN", 1)
        s.commit()

    results: list[str] = []
    a_holds_lock = threading.Event()
    release_a = threading.Event()

    def seller_a():
        with Session(engine) as s:
            move(s, sku, lot, "sale", "US_MAIN", "CUSTOMER", 1, key="concurrent-a")
            a_holds_lock.set()
            release_a.wait(5)
            s.commit()
            results.append("a:ok")

    def seller_b():
        a_holds_lock.wait(5)
        with Session(engine) as s:
            try:
                # Blocks on A's row lock, then sees qty 0 after A commits.
                move(s, sku, lot, "sale", "US_MAIN", "CUSTOMER", 1, key="concurrent-b")
                s.commit()
                results.append("b:ok")
            except IntegrityError:
                s.rollback()
                results.append("b:rejected")

    ta, tb = threading.Thread(target=seller_a), threading.Thread(target=seller_b)
    ta.start(); tb.start()
    a_holds_lock.wait(5)
    release_a.set()
    ta.join(10); tb.join(10)

    assert sorted(results) == ["a:ok", "b:rejected"]
    with Session(engine) as s:
        qty = s.execute(text("SELECT us_on_hand FROM v_inventory_position WHERE sku_id = :s"), {"s": sku}).scalar_one()
        sold = s.execute(text("SELECT count(*) FROM inventory_movements WHERE sku_id = :s AND movement_type = 'sale'"),
                         {"s": sku}).scalar_one()
    assert (qty, sold) == (0, 1)
