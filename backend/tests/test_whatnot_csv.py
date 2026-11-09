"""Whatnot show-export CSV import. The header row is exactly the one of a real export
(live-<show uuid>.csv, 2026-11); all values here are made up for the test."""
import csv
import io
from decimal import Decimal

from sqlalchemy import select, text

from app.integrations.whatnot import processing as P
from app.models.sales import SalesOrderLine
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product

HEADER = ["order_id", "order_numeric_id", "buyer_username", "product_name", "product_description", "product_quantity",
          "original_item_price", "placed_at", "cancelled_or_failed", "bundled", "shipment_manifest", "label_only",
          "tracking_code", "shipment_id", "shipping_address", "postal_code", "coupon_code", "coupon_price", "gifted_to",
          "sku", "cost_per_item", "total_cost"]
SHOW = "91cbe615-0000-41e3-8d9a-cb0e91bdee5b"


def row(num, title, price, *, flag="", placed="2026-11-08 03:31:23.705", buyer="test_buyer_1"):
    return [f"Ab{num}", str(num), buyer, title, title.rsplit(" #", 1)[0], "1", f"{price:.1f}",
            placed if flag != "failed" else "", flag, "True", "", "", "9400TEST", "SHIP1",
            "1 Test Street, Testville", "00000", "", "", "", "", "0.0", "0.0"]


def csv_bytes(rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    w.writerows(rows)
    return buf.getvalue().encode()


def upload(api, tok, data, name=f"live-{SHOW}_1.csv"):
    return api.post("/api/v1/imports", headers=auth_header(tok), data={"import_type": "whatnot_orders"},
                    files={"file": (name, data, "text/csv")})


def rows_of(api, tok, job_id):
    return {r["row_number"]: r for r in api.get(f"/api/v1/imports/{job_id}/rows", headers=auth_header(tok)).json()}


def test_export_is_detected_and_personal_data_is_not_stored(api, db):
    _, tok = token_for(api, db, *EDITOR)
    data = csv_bytes([row(1001, "1x PERONA OP14-111 PROMO #1", 12), row(1002, "1x RANDOM PACK JP #1", 0),
                      row(1003, "1x PERONA OP14-111 PROMO #2", 15, flag="failed"),
                      row(1004, "1x NAMI EB03-006 PROMO #1", 9, flag="cancelled")])
    r = upload(api, tok, data)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "validated"
    assert set(job["mapping"]["columns"]) >= {"order_id", "title", "qty", "price", "placed_at", "status_flag"}
    assert job["headers"][job["mapping"]["columns"]["order_id"]] == "order_numeric_id"
    rows = rows_of(api, tok, job["id"])
    assert rows[2]["action"] == "import_unmapped" and rows[3]["action"] == "import_unmapped"
    assert any("贈品" in w for w in rows[3]["warnings"])
    assert rows[4]["action"] == "import_unpaid" and rows[5]["action"] == "import_unpaid"
    # no buyer data in staged rows nor in the stored preview
    staged = str(db.execute(text("SELECT json_agg(raw) FROM import_rows WHERE job_id = :j"), {"j": job["id"]}).scalar_one())
    preview = str(job["sheets"])
    for secret in ("test_buyer_1", "Testville", "00000"):
        assert secret not in staged and secret not in preview


def test_commit_creates_orders_for_the_show_and_deletes_the_file(api, db):
    from app.services import storage
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("PERONA-PROMO",))["sku_ids"][0]
    api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()},
             json={"location_code": "US_MAIN", "lines": [{"sku_id": sku, "qty": 10, "unit_cost_usd": "2"}]})
    data = csv_bytes([row(2001, "1x PERONA OP14-111 PROMO #1", 12), row(2002, "1x PERONA OP14-111 PROMO #2", 15),
                      row(2003, "1x RANDOM PACK JP #7", 0), row(2004, "1x NAMI PROMO #1", 9, flag="failed")])
    job = upload(api, tok, data).json()
    key = db.execute(text("SELECT storage_key FROM import_jobs WHERE id = :j"), {"j": job["id"]}).scalar_one()
    r = api.post(f"/api/v1/imports/{job['id']}/commit", headers=auth_header(tok), json={})
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert (s["orders"], s["revenue_usd"], s["unmapped_lines"], s["show_id"]) == (4, "27.00", 3, SHOW)
    assert not (storage._root() / key).exists()  # file with buyer data removed
    show = db.execute(text("""SELECT count(*) FROM sales_orders o JOIN livestreams l ON l.id = o.livestream_id
                              WHERE l.external_id = :s"""), {"s": SHOW}).scalar_one()
    assert show == 4
    # map once -> every PERONA auction (#1, #2, future #N) follows
    groups = api.get("/api/v1/orders/unmapped-products", headers=auth_header(tok)).json()
    perona = next(g for g in groups if g["title"].startswith("1x PERONA"))
    assert (perona["lines"], perona["units"]) == (2, 2)
    m = api.post(f"/api/v1/orders/lines/{perona['example_line_id']}/map", headers=auth_header(tok), json={"sku_id": sku})
    assert m.status_code == 200, m.text
    reserved = db.execute(text("SELECT us_reserved FROM v_inventory_position WHERE sku_id = :s"), {"s": sku}).scalar_one()
    assert reserved == 2
    P.process_order(db, W.order("3001"), [W.item("I-3001", "3001", title="1x PERONA OP14-111 PROMO #9",
                                                product_id=None)], source="api")
    assert db.scalar(select(SalesOrderLine.sku_id).where(SalesOrderLine.external_line_id == "I-3001")) == sku


def test_csv_then_api_does_not_double_count(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("DBL-1",))["sku_ids"][0]
    api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()},
             json={"location_code": "US_MAIN", "lines": [{"sku_id": sku, "qty": 5, "unit_cost_usd": "2"}]})
    data = csv_bytes([row(4001, "DBL-1 card #1", 20)])
    rows = list(csv.reader(io.StringIO(data.decode())))
    rows[1][HEADER.index("sku")] = "DBL-1"  # listing had a SKU this time
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    job = upload(api, tok, buf.getvalue().encode()).json()
    assert api.post(f"/api/v1/imports/{job['id']}/commit", headers=auth_header(tok), json={}).status_code == 200
    # later the API sync sees the same order (its own item id): the CSV line is taken over
    P.process_order(db, W.order("4001", subtotal=20, updated="2026-11-08T21:00:00Z"),
                    [W.item("API-ITEM-9", "4001", price=20, sku="DBL-1")], source="api")
    revenue = db.execute(text("""SELECT sum(e.amount_base) FROM financial_entries e JOIN sales_orders o ON o.id = e.sales_order_id
                                 WHERE o.external_order_id = '4001' AND e.entry_type = 'revenue'""")).scalar_one()
    assert revenue == Decimal("20.0000")
    lines = list(db.execute(text("""SELECT x.external_line_id FROM sales_order_lines x JOIN sales_orders o ON o.id = x.order_id
                                    WHERE o.external_order_id = '4001'""")).scalars())
    assert lines == ["API-ITEM-9"]
    assert db.execute(text("SELECT us_reserved FROM v_inventory_position WHERE sku_id = :s"), {"s": sku}).scalar_one() == 1
    # and re-uploading the export later skips orders already in the ERP
    job2 = upload(api, tok, buf.getvalue().encode() + b"\n").json()
    assert rows_of(api, tok, job2["id"])[2]["action"] == "skip_existing"


def test_mark_shipped_option_moves_stock(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("SHIPPED-1",))["sku_ids"][0]
    api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()},
             json={"location_code": "US_MAIN", "lines": [{"sku_id": sku, "qty": 3, "unit_cost_usd": "2"}]})
    data = csv_bytes([row(5001, "SHIPPED-1 #1", 10), row(5002, "SHIPPED-1 #2", 11)])
    rows = list(csv.reader(io.StringIO(data.decode())))
    for r in rows[1:]:
        r[HEADER.index("sku")] = "SHIPPED-1"
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    job = upload(api, tok, buf.getvalue().encode()).json()
    r = api.post(f"/api/v1/imports/{job['id']}/commit", headers=auth_header(tok), json={"mark_shipped": True})
    assert r.status_code == 200
    p = db.execute(text("SELECT us_on_hand, us_reserved FROM v_inventory_position WHERE sku_id = :s"), {"s": sku}).one()
    assert tuple(p) == (1, 0)


def test_order_import_permission(api, db):
    _, jp = token_for(api, db, "jp_inventory")  # no import.sales
    assert upload(api, jp, csv_bytes([row(1, "x #1", 1)])).status_code == 403
    _, us = token_for(api, db, "us_live_ops")
    assert upload(api, us, csv_bytes([row(1, "x #1", 1)])).status_code == 201
