"""Phase 4 import tests. Workbooks are generated with the same layout as the owner's template
(Whatnot_ERP_CSV_Excel_Template_EN.xlsx): extra sheets, formula columns never calculated by
Excel, an example row, etc."""
import io
from decimal import Decimal

import openpyxl
from sqlalchemy import text

from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product

COST_HEADERS = ["Cost Batch ID", "SKU", "Product Name", "Purchase Date", "Purchase Quantity", "Unit Purchase Cost",
                "Purchase Currency", "Exchange Rate to USD", "Purchase Cost (USD)", "Domestic Japan Shipping (USD)",
                "International Freight Allocation (USD)", "Duties/Other Costs (USD)", "Total Batch Cost (USD)",
                "Landed Cost per Unit (USD)", "Costing Method (FIFO/Weighted Average)", "Supplier", "Source Filename",
                "Notes"]
EXAMPLE = ["LOT-EXAMPLE", "SKU-001", "Sample Product - Replace", None, 10, 500, "JPY", 0.0067, "=F2*H2*E2", 2, 5, 1,
           "=I2+J2+K2+L2", "=IF(E2=0,0,M2/E2)", "FIFO", None, None, "Example row; exchange rate is illustrative"]
COSTER = (*EDITOR,)  # jp_inventory has import.cost; tw_manager too


def cost_row(batch, sku, name, d, qty, unit, cur, fx, dom=0, intl=0, duty=0, method="FIFO", supplier="Tokyo Wholesale",
             r=None):
    r = r or 3
    return [batch, sku, name, d, qty, unit, cur, fx, f"=F{r}*H{r}*E{r}", dom, intl, duty, f"=I{r}+J{r}+K{r}+L{r}",
            f"=IF(E{r}=0,0,M{r}/E{r})", method, supplier, None, None]


def workbook(rows, headers=COST_HEADERS, title="Purchase Cost Batches", with_other_sheets=True) -> bytes:
    wb = openpyxl.Workbook()
    ws0 = wb.active
    ws0.title = "Instructions"
    ws0.append(["Whatnot CSV / ERP Excel Template", None])
    if with_other_sheets:
        o = wb.create_sheet("Order Details")
        o.append(["Order ID", "Order Item ID", "SKU", "Quantity", "Unit Price"])
    ws = wb.create_sheet(title)
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(api, tok, data, name="costs.xlsx", import_type="purchase_cost"):
    return api.post("/api/v1/imports", headers=auth_header(tok), data={"import_type": import_type},
                    files={"file": (name, data, "application/octet-stream")})


def rows_of(api, tok, job_id, flt="all"):
    return {r["row_number"]: r for r in api.get(f"/api/v1/imports/{job_id}/rows", headers=auth_header(tok),
                                                params={"filter": flt}).json()}


def commit(api, tok, job_id, **opts):
    return api.post(f"/api/v1/imports/{job_id}/commit", headers=auth_header(tok), json=opts)


def test_template_layout_detected_example_skipped_and_costs_computed(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("PK151-BOX",))
    data = workbook([EXAMPLE, cost_row("B-2026-001", "pk151-box", "151 Box", "2026-10-20", 10, 500, "JPY", 0.0067,
                                       dom=2, intl=5, duty=1)])
    r = upload(api, tok, data)
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "validated"
    assert job["mapping"]["sheet"] == "Purchase Cost Batches" and job["mapping"]["header_row"] == 0
    assert len(job["mapping"]["columns"]) == 17  # every template column except "Source Filename"
    rows = rows_of(api, tok, job["id"])
    assert rows[2]["action"] == "skip_example"
    real = rows[3]
    assert real["action"] == "import" and real["errors"] == []
    nz = real["normalized"]
    # 10 x 500 JPY x 0.0067 = 33.50 ; + 2 + 5 + 1 = 41.50 ; / 10 = 4.15
    assert (nz["purchase_cost_usd"], nz["landed_total_usd"], nz["landed_unit_usd"]) == ("33.5000", "41.5000", "4.150000")
    assert job["stats"]["by_action"] == {"skip_example": 1, "import": 1}


def test_commit_writes_frozen_cost_records_but_no_stock(api, db):
    _, tok = token_for(api, db, *COSTER)
    sid = create_product(api, tok, codes=("OP09-BOX",))["sku_ids"][0]
    data = workbook([cost_row("B-OP09-01", "OP09-BOX", "OP09", "2026-10-01", 20, 6800, "JPY", "0.0066", intl=12)])
    job = upload(api, tok, data).json()
    res = commit(api, tok, job["id"])
    assert res.status_code == 200, res.text
    assert res.json()["summary"] == {"batches": 1, "lines": 1, "new_skus": 0, "landed_total_usd": "909.60",
                                     "skipped_error_rows": 0}
    batch = db.execute(text("SELECT id, status FROM purchase_batches WHERE batch_no = 'B-OP09-01'")).one()
    assert batch.status == "confirmed"
    line = db.execute(text("SELECT * FROM purchase_lines WHERE batch_id = :b"), {"b": batch.id}).mappings().one()
    assert line["landed_unit_cost_base"] == Decimal("45.480000") and line["source_import_row_id"] is not None
    # importing cost does NOT create stock
    jp = api.get(f"/api/v1/skus/{sid}", headers=auth_header(tok)).json()["jp_on_hand"]
    assert jp == 0
    # the same file can never be imported twice
    again = upload(api, tok, data)
    assert again.status_code == 409 and again.json()["error"]["code"] == "file_already_imported"
    # the same batch ID in a different file is rejected (history is not overwritten)
    other = upload(api, tok, workbook([cost_row("b-op09-01", "OP09-BOX", "OP09", "2026-10-01", 1, 1, "USD", 1)])).json()
    assert "已經匯入過" in rows_of(api, tok, other["id"])[2]["errors"][0]


def test_new_sku_requires_confirmation(api, db):
    _, tok = token_for(api, db, *COSTER)
    job = upload(api, tok, workbook([cost_row("B-NEW", "NEW-SKU-1", "Dragon Ball Box", "2026-09-01", 5, 4000, "JPY",
                                              0.0068)])).json()
    row = rows_of(api, tok, job["id"])[2]
    assert row["action"] == "import_new_sku" and "建立新商品" in row["warnings"][0]
    r = commit(api, tok, job["id"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "new_skus_not_confirmed"
    r = commit(api, tok, job["id"], create_missing_skus=True)
    assert r.status_code == 200 and r.json()["summary"]["new_skus"] == 1
    skus = api.get("/api/v1/skus", headers=auth_header(tok), params={"search": "NEW-SKU-1"}).json()
    assert skus[0]["name"] == "Dragon Ball Box"


def test_exchange_rate_rules(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("FX-1",))
    job = upload(api, tok, workbook([
        cost_row("FX-A", "FX-1", "x", "2026-10-01", 1, 1000, "JPY", 150, r=2),       # inverted
        cost_row("FX-B", "FX-1", "x", "2026-10-01", 1, 1000, "JPY", None, r=3),      # missing
        cost_row("FX-C", "FX-1", "x", "2026-10-01", 1, 25, "USD", None, r=4),        # USD -> 1
        cost_row("FX-D", "FX-1", "x", "2026-10-01", 1, 25, "US$", 1.2, r=5),         # USD must be 1
        cost_row("FX-E", "FX-1", "x", "2026-10-01", 1, 300, "台幣", 0.031, r=6),      # alias for TWD
    ])).json()
    rows = rows_of(api, tok, job["id"])
    assert "寫反" in rows[2]["errors"][0] and "0.006667" in rows[2]["errors"][0]
    assert "不會自行假設匯率" in rows[3]["errors"][0]
    assert rows[4]["errors"] == [] and rows[4]["normalized"]["fx_rate"] == "1"
    assert "匯率應為 1" in rows[5]["errors"][0]
    assert rows[6]["errors"] == [] and rows[6]["normalized"]["currency"] == "TWD"


def test_errors_block_commit_unless_skipped(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("ERR-1",))
    job = upload(api, tok, workbook([
        cost_row("E-1", "ERR-1", "x", "2026-10-01", 3, 10, "USD", 1, r=2),
        cost_row("E-2", "ERR-1", "x", "not a date", -1, "abc", "EUR", 1, r=3),
    ])).json()
    bad = rows_of(api, tok, job["id"], "errors")[3]["errors"]
    assert any("日期" in e for e in bad) and any("數量" in e for e in bad) and any("單價" in e for e in bad)
    assert any("EUR" in e for e in bad)
    r = commit(api, tok, job["id"])
    assert r.status_code == 422 and r.json()["error"]["details"]["error_rows"] == [3]
    r = commit(api, tok, job["id"], skip_error_rows=True)
    assert r.status_code == 200 and r.json()["summary"]["lines"] == 1 and r.json()["summary"]["skipped_error_rows"] == 1


def test_batch_consistency_and_duplicates(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("BC-1", "BC-2"))
    job = upload(api, tok, workbook([
        cost_row("MIX", "BC-1", "x", "2026-10-01", 1, 10, "USD", 1, r=2),
        cost_row("MIX", "BC-2", "x", "2026-10-05", 1, 10, "USD", 1, r=3),
        cost_row("DUP", "BC-1", "x", "2026-10-01", 1, 10, "USD", 1, r=4),
        cost_row("DUP", "bc-1", "x", "2026-10-01", 1, 10, "USD", 1, r=5),
        cost_row("OK2", "BC-1", "x", "2026-10-01", 2, 10, "USD", 1, r=6),
        cost_row("OK2", "BC-2", "x", "2026-10-01", 2, 10, "USD", 1, r=7),
    ])).json()
    rows = rows_of(api, tok, job["id"])
    assert "必須相同" in rows[2]["errors"][0] and "必須相同" in rows[3]["errors"][0]
    assert "重複" in rows[5]["errors"][0]
    r = commit(api, tok, job["id"], skip_error_rows=True)
    assert r.json()["summary"]["batches"] == 2 and r.json()["summary"]["lines"] == 3  # DUP row 4 + OK2 (2 lines)


def test_cross_check_warning_and_weighted_average_notice(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("CC-1",))
    row = cost_row("CC", "CC-1", "x", "2026-10-01", 4, 10, "USD", 1, dom=2, method="Weighted Average", r=2)
    row[13] = 9.99  # sheet says 9.99 per unit; real landed cost is (40 + 2) / 4 = 10.50
    job = upload(api, tok, workbook([row])).json()
    w = rows_of(api, tok, job["id"])[2]["warnings"]
    assert any("FIFO" in x for x in w) and any("9.99" in x and "10.50" in x for x in w)


def test_uncalculated_formula_in_input_column_is_reported(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("FM-1",))
    row = cost_row("FM", "FM-1", "x", "2026-10-01", "=2*5", 10, "USD", 1, r=2)
    job = upload(api, tok, workbook([row])).json()
    assert any("公式沒有計算結果" in e for e in rows_of(api, tok, job["id"])[2]["errors"])


def test_japanese_csv_in_shift_jis(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("JP-CSV-1",))
    csv_text = "品番,商品名,仕入日,数量,単価,通貨,為替レート,国際送料\nJP-CSV-1,テスト,2026/10/03,6,\"1,200\",円,0.0067,3.00\n"
    job = upload(api, tok, csv_text.encode("cp932"), name="仕入.csv").json()
    assert job["status"] == "validated", job
    row = rows_of(api, tok, job["id"])[2]
    assert row["errors"] == [] and row["normalized"]["currency"] == "JPY" and row["normalized"]["unit_cost"] == "1200"
    assert row["normalized"]["landed_total_usd"] == "51.2400"  # 6 x 1200 x 0.0067 + 3


def test_manual_mapping_and_remembered_layout(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("MAP-1",))
    headers = ["Code", "When", "Pieces", "Each", "Money", "Rate"]
    data = workbook([["MAP-1", "2026-10-01", 3, 100, "JPY", 0.0067]], headers=headers, title="Sheet1",
                    with_other_sheets=False)
    job = upload(api, tok, data, name="weird.xlsx").json()
    assert job["status"] == "parsed" and job["stats"]["missing_fields"]
    cols = {"sku_code": 0, "purchase_date": 1, "qty": 2, "unit_cost": 3, "currency": 4, "fx_rate": 5}
    r = api.put(f"/api/v1/imports/{job['id']}/mapping", headers=auth_header(tok),
                json={"sheet": "Sheet1", "header_row": 0, "columns": cols})
    assert r.status_code == 200 and r.json()["status"] == "validated"
    assert commit(api, tok, job["id"]).status_code == 200
    # next file with the same header layout is mapped automatically
    data2 = workbook([["MAP-1", "2026-10-02", 1, 100, "JPY", 0.0067]], headers=headers, title="Sheet1",
                     with_other_sheets=False)
    job2 = upload(api, tok, data2, name="weird2.xlsx").json()
    assert job2["status"] == "validated" and job2["mapping"]["remembered"] is True


def test_bundle_cannot_be_purchased(api, db):
    _, tok = token_for(api, db, *COSTER)
    comp = create_product(api, tok, codes=("BP-C",))["sku_ids"][0]
    api.post("/api/v1/bundles", headers=auth_header(tok),
             json={"sku_code": "BP-SET", "name": "set", "components": [{"sku_id": comp, "qty": 1}]})
    job = upload(api, tok, workbook([cost_row("BP", "BP-SET", "set", "2026-10-01", 1, 10, "USD", 1, r=2)])).json()
    assert "組合商品不能直接採購" in rows_of(api, tok, job["id"])[2]["errors"][0]


def test_receive_into_japan_creates_fifo_lots(api, db):
    _, tok = token_for(api, db, *COSTER)
    sid = create_product(api, tok, codes=("RCV-1",))["sku_ids"][0]
    job = upload(api, tok, workbook([cost_row("RCV-B", "RCV-1", "x", "2026-10-01", 10, 1000, "JPY", 0.0067, intl=3,
                                              r=2)])).json()
    commit(api, tok, job["id"])
    batch = [b for b in api.get("/api/v1/purchase-batches", headers=auth_header(tok)).json() if b["batch_no"] == "RCV-B"][0]
    assert (batch["qty"], batch["received"]) == (10, 0)
    line = api.get(f"/api/v1/purchase-batches/{batch['id']}", headers=auth_header(tok)).json()["lines"][0]
    url = f"/api/v1/purchase-lines/{line['id']}/receive"
    key = idem()
    r1 = api.post(url, headers={**auth_header(tok), **key}, json={"qty": 4, "received_on": "2026-10-10"})
    r1b = api.post(url, headers={**auth_header(tok), **key}, json={"qty": 4, "received_on": "2026-10-10"})
    assert r1.status_code == 201 and r1.json() == r1b.json()  # double tap: received once
    over = api.post(url, headers={**auth_header(tok), **idem()}, json={"qty": 7})
    assert over.status_code == 409 and over.json()["error"]["details"]["already_received"] == 4
    assert api.post(url, headers={**auth_header(tok), **idem()}, json={"qty": 6}).status_code == 201
    d = api.get(f"/api/v1/skus/{sid}", headers=auth_header(tok)).json()
    assert d["jp_on_hand"] == 10
    # landed unit cost = (10 x 1000 x 0.0067 + 3) / 10 = (67 + 3) / 10 = 7.00
    lots = [(l["jp"], l["unit_cost_base"]) for l in d["lots"]]
    assert lots == [(4, "7.000000"), (6, "7.000000")], lots
    assert d["lots"][0]["received_at"].startswith("2026-10-10")
    _, us = token_for(api, db, "us_live_ops")
    assert api.post(url, headers={**auth_header(us), **idem()}, json={"qty": 1}).status_code == 403


def test_import_permissions(api, db):
    _, us = token_for(api, db, "us_live_ops")
    r = upload(api, us, workbook([]))
    assert r.status_code == 403
    _, ro = token_for(api, db, "read_only")
    assert upload(api, ro, workbook([]), import_type="sku_master").status_code == 403


SKU_HEADERS = ["SKU", "Product Name", "Category", "Variant/Specification", "Whatnot Listing ID", "Default Selling Price",
               "Selling Currency", "Current Standard Cost (USD)", "Safety Stock", "Japan Inventory", "US Inventory",
               "In-Transit Inventory", "Reserved Inventory", "Available US Inventory", "Product Status", "Notes"]


def test_sku_master_import(api, db):
    _, tok = token_for(api, db, *COSTER)
    existing = create_product(api, tok, codes=("SM-OLD",), name="Old name")["sku_ids"][0]
    data = workbook([
        ["SKU-001", "Sample Product - Replace", None, None, None, 10, "USD", 4, 5, 20, 10, 0, 0, "=K2-M2", "Active", "Example row"],
        ["SM-NEW", "Yu-Gi-Oh Box", "Trading Cards", "JP", "WN-LIST-77", 59.9, "USD", 30, 3, 999, 999, 0, 0, "=K3-M3", "Active", None],
        ["SM-OLD", "New name", None, None, None, 12, "USD", None, 2, 5, 5, 0, 0, None, "Discontinued", None],
    ], headers=SKU_HEADERS, title="Product SKU Master")
    job = upload(api, tok, data, import_type="sku_master").json()
    assert job["mapping"]["sheet"] == "Product SKU Master"
    rows = rows_of(api, tok, job["id"])
    assert (rows[2]["action"], rows[3]["action"], rows[4]["action"]) == ("skip_example", "create", "update")
    r = commit(api, tok, job["id"])
    assert r.status_code == 200 and r.json()["summary"] == {"created": 1, "updated": 1, "skipped_error_rows": 0}
    new = api.get("/api/v1/skus", headers=auth_header(tok), params={"search": "SM-NEW"}).json()[0]
    assert new["category_name"] == "Trading Cards" and new["list_price"] == "59.9000"
    assert new["jp_on_hand"] == 0 and new["us_on_hand"] == 0  # inventory columns are NOT imported
    old = api.get(f"/api/v1/skus/{existing}", headers=auth_header(tok)).json()
    assert (old["name"], old["status"], old["safety_stock_override"]) == ("New name", "discontinued", 2)
    mapped = db.execute(text("SELECT sku_id FROM external_sku_mappings WHERE external_product_id = 'WN-LIST-77'")).scalar_one()
    assert mapped == new["id"]


def test_cancel_and_closed_jobs(api, db):
    _, tok = token_for(api, db, *COSTER)
    create_product(api, tok, codes=("CN-1",))
    job = upload(api, tok, workbook([cost_row("CN", "CN-1", "x", "2026-10-01", 1, 10, "USD", 1, r=2)])).json()
    assert api.post(f"/api/v1/imports/{job['id']}/cancel", headers=auth_header(tok)).json()["status"] == "cancelled"
    assert commit(api, tok, job["id"]).status_code == 409
    assert db.execute(text("SELECT count(*) FROM purchase_batches WHERE batch_no = 'CN'")).scalar_one() == 0
