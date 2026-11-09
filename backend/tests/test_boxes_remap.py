from decimal import Decimal

from sqlalchemy import select, text

from app.integrations.whatnot import processing as P
from app.models.sales import SalesOrderLine
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product


def us_stock(api, tok, sku, qty, cost):
    r = api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()},
                 json={"location_code": "US_MAIN", "lines": [{"sku_id": sku, "qty": qty, "unit_cost_usd": cost}]})
    assert r.status_code == 201, r.text


def pos(db, sku):
    return db.execute(text("SELECT * FROM v_inventory_position WHERE sku_id = :s"), {"s": sku}).mappings().one()


def value(db, sku):
    return db.execute(text("SELECT COALESCE(sum(value_base), 0) FROM v_inventory_valuation WHERE sku_id = :s"),
                      {"s": sku}).scalar_one()


def make_box(api, tok):
    box, promo, pack, sleeve = create_product(api, tok, codes=("GC-BOX", "GC-PROMO", "GC-PACK", "GC-SLEEVE"))["sku_ids"]
    d = api.get(f"/api/v1/skus/{box}", headers=auth_header(tok)).json()
    r = api.put(f"/api/v1/skus/{box}/box-contents", headers=auth_header(tok), json={
        "version": d["version"], "contents": [{"sku_id": promo, "qty": 8}, {"sku_id": pack, "qty": 2},
                                              {"sku_id": sleeve, "qty": 1}]})
    assert r.status_code == 200, r.text
    assert sum(c["qty"] for c in r.json()["box_contents"]) == 11
    return box, promo, pack, sleeve


def test_open_boxes_splits_cost_equally_fifo(api, db):
    _, tok = token_for(api, db, *EDITOR)
    box, promo, pack, sleeve = make_box(api, tok)
    us_stock(api, tok, box, 2, "110")
    us_stock(api, tok, box, 1, "121")
    key = idem()
    r = api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **key},
                 json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 3, "note": "opened on stream"})
    again = api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **key},
                     json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 3})
    assert r.status_code == 201 and r.json()["id"] == again.json()["id"]  # double tap: opened once
    assert pos(db, box)["us_on_hand"] == 0
    assert (pos(db, promo)["us_on_hand"], pos(db, pack)["us_on_hand"], pos(db, sleeve)["us_on_hand"]) == (24, 6, 3)
    # $110 boxes -> $10 per unit ; $121 box -> $11 per unit ; total cost conserved: 3 boxes = $341
    lots = db.execute(text("""SELECT original_qty, original_unit_cost_base, parent_lot_id IS NOT NULL
                              FROM inventory_lots WHERE sku_id = :s ORDER BY id"""), {"s": promo}).all()
    assert [(q, c, p) for q, c, p in lots] == [(16, Decimal("10.000000"), True), (8, Decimal("11.000000"), True)]
    total = sum(value(db, s) for s in (promo, pack, sleeve))
    assert total == Decimal("341.0000")
    hist = api.get("/api/v1/inventory/box-openings", headers=auth_header(tok), params={"box_sku_id": box}).json()
    assert hist[0]["qty"] == 3 and hist[0]["units_per_box"] == 11


def test_box_rules(api, db):
    _, tok = token_for(api, db, *EDITOR)
    box, promo, *_ = make_box(api, tok)
    plain = create_product(api, tok, codes=("NOBOX",))["sku_ids"][0]
    us_stock(api, tok, plain, 1, "5")
    r = api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **idem()},
                 json={"box_sku_id": plain, "location_code": "US_MAIN", "qty": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_box_contents"
    r = api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **idem()},
                 json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "insufficient_stock"
    d = api.get(f"/api/v1/skus/{box}", headers=auth_header(tok)).json()
    r = api.put(f"/api/v1/skus/{box}/box-contents", headers=auth_header(tok),
                json={"version": d["version"], "contents": [{"sku_id": box, "qty": 1}]})
    assert r.status_code == 422
    _, ro = token_for(api, db, "read_only")
    r = api.post("/api/v1/inventory/box-openings", headers={**auth_header(ro), **idem()},
                 json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 1})
    assert r.status_code == 403


def test_selling_an_opened_card_uses_its_box_cost(api, db):
    _, tok = token_for(api, db, *EDITOR)
    box, promo, *_ = make_box(api, tok)
    us_stock(api, tok, box, 1, "110")
    api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **idem()},
             json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 1})
    P.process_order(db, W.order("BX-1", status="completed", subtotal=25),
                    [W.item("BX-I1", "BX-1", price=25, ext="GC-PROMO")], source="api")
    cogs = db.execute(text("""SELECT sum(amount_base) FROM financial_entries e JOIN sales_orders o ON o.id = e.sales_order_id
                              WHERE o.external_order_id = 'BX-1' AND e.entry_type = 'cogs'""")).scalar_one()
    assert cogs == Decimal("-10.0000")


def net(db, ext):
    return db.execute(text("""SELECT entry_type, sum(amount_base) FROM financial_entries e JOIN sales_orders o
                              ON o.id = e.sales_order_id WHERE o.external_order_id = :o GROUP BY 1"""), {"o": ext}).all()


def test_correcting_a_wrong_sku_reverses_stock_and_cost(api, db):
    _, tok = token_for(api, db, *EDITOR)
    wrong, right = create_product(api, tok, codes=("WRONG-1", "RIGHT-1"))["sku_ids"]
    us_stock(api, tok, wrong, 5, "4")
    us_stock(api, tok, right, 5, "9")
    for n in (1, 2):  # two auctions of the same Whatnot product, both shipped
        P.process_order(db, W.order(f"RM-{n}", status="completed", subtotal=30),
                        [W.item(f"RM-I{n}", f"RM-{n}", price=30, product_id="WP-55", title=f"Promo X #{n}")],
                        source="api")
    other = W.item("RM-I9", "RM-9", price=30, product_id="WP-99", title="Other product #1")
    P.process_order(db, W.order("RM-9", status="pending_fulfillment", subtotal=30), [other], source="api")
    l1 = db.scalar(select(SalesOrderLine).where(SalesOrderLine.external_line_id == "RM-I1"))
    db.commit()
    # map to the WRONG SKU first (as the e2e mistake did), then also the other product
    assert api.post(f"/api/v1/orders/lines/{l1.id}/map", headers=auth_header(tok), json={"sku_id": wrong}).status_code == 200
    l9 = db.scalar(select(SalesOrderLine).where(SalesOrderLine.external_line_id == "RM-I9"))
    api.post(f"/api/v1/orders/lines/{l9.id}/map", headers=auth_header(tok), json={"sku_id": wrong})
    assert pos(db, wrong)["us_on_hand"] == 3 and pos(db, wrong)["us_reserved"] == 1
    assert dict(net(db, "RM-1")) == {"revenue": Decimal("30.0000"), "cogs": Decimal("-4.0000")}

    r = api.post(f"/api/v1/orders/lines/{l1.id}/remap", headers=auth_header(tok), json={"sku_id": right})
    assert r.status_code == 200, r.text
    # both auctions of WP-55 moved; the other product (still reserved on WRONG-1) untouched
    assert (pos(db, wrong)["us_on_hand"], pos(db, wrong)["us_reserved"]) == (5, 1)
    assert pos(db, right)["us_on_hand"] == 3
    assert dict(net(db, "RM-1")) == {"revenue": Decimal("30.0000"), "cogs": Decimal("-9.0000")}  # 4 reversed, 9 booked
    assert float(r.json()["lines"][0]["cogs"]) == 9.0
    # a future order of the product follows the corrected rule
    P.process_order(db, W.order("RM-3"), [W.item("RM-I3", "RM-3", product_id="WP-55", title="Promo X #3")], source="api")
    assert db.scalar(select(SalesOrderLine.sku_id).where(SalesOrderLine.external_line_id == "RM-I3")) == right
    # and back again (generations keep keys unique)
    api.post(f"/api/v1/orders/lines/{l1.id}/remap", headers=auth_header(tok), json={"sku_id": wrong})
    assert dict(net(db, "RM-1")) == {"revenue": Decimal("30.0000"), "cogs": Decimal("-4.0000")}
    assert db.execute(text("SELECT count(*) FROM v_inventory_reconciliation")).scalar_one() == 0


def test_unmap_and_permission(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("UNMAP-1",))["sku_ids"][0]
    us_stock(api, tok, sku, 2, "4")
    P.process_order(db, W.order("UM-1", status="completed"), [W.item("UM-I1", "UM-1", ext="UNMAP-1")], source="api")
    line = db.scalar(select(SalesOrderLine).where(SalesOrderLine.external_line_id == "UM-I1"))
    db.commit()
    _, catalog_only = token_for(api, db, "jp_inventory")  # catalog.edit but no US stock permission
    assert api.post(f"/api/v1/orders/lines/{line.id}/remap", headers=auth_header(catalog_only),
                    json={"sku_id": None}).status_code == 403
    r = api.post(f"/api/v1/orders/lines/{line.id}/remap", headers=auth_header(tok), json={"sku_id": None})
    assert r.status_code == 200
    l = r.json()["lines"][0]
    assert l["sku_id"] is None and l["exception_code"] == "unmapped_sku" and pos(db, sku)["us_on_hand"] == 2
