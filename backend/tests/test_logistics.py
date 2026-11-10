"""Phase 9: inbound parcels (Japan -> US), transit loss, lead time, outbound backlog, inventory gains/losses in the P&L."""
from decimal import Decimal

from sqlalchemy import text

from app.integrations.whatnot import processing as P
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import create_product, skus_by_code
from tests.test_imports import commit, cost_row, upload, workbook
from tests.test_whatnot import us_stock


def batch_lines(api, db):
    """Two SKUs bought in one batch (10 + 4 units), imported from a cost sheet."""
    _, jp = token_for(api, db, "jp_inventory", "tw_manager")
    create_product(api, jp, codes=("LG-A", "LG-B"))
    rows = [cost_row("LG-BATCH", "LG-A", "a", "2026-10-01", 10, 1500, "JPY", 150, r=2),
            cost_row("LG-BATCH", "LG-B", "b", "2026-10-01", 4, 3000, "JPY", 150, r=3)]
    job = upload(api, jp, workbook(rows)).json()
    assert commit(api, jp, job["id"]).status_code == 200
    batch = next(b for b in api.get("/api/v1/purchase-batches", headers=auth_header(jp)).json() if b["batch_no"] == "LG-BATCH")
    lines = {l["sku_code"]: l for l in api.get(f"/api/v1/purchase-batches/{batch['id']}", headers=auth_header(jp)).json()["lines"]}
    return jp, lines


def pos(db, sku):
    return db.execute(text("SELECT * FROM v_inventory_position WHERE sku_id = :s"), {"s": sku}).mappings().one()


def carrier(db, name="FedEx"):
    return db.execute(text("SELECT id FROM carriers WHERE name = :n"), {"n": name}).scalar_one()


def test_parcel_ship_receive_partial_and_loss(api, db):
    jp, lines = batch_lines(api, db)
    a, b = lines["LG-A"], lines["LG-B"]
    _, us = token_for(api, db, "us_live_ops")
    body = {"lines": [{"purchase_line_id": a["id"], "qty": 6}, {"purchase_line_id": b["id"], "qty": 4}],
            "shipped_on": "2026-10-20", "carrier_id": carrier(db), "tracking_no": "7712 3456", "packages": 2}
    assert api.post("/api/v1/parcels", headers={**auth_header(us), **idem()}, json=body).status_code == 403
    key = idem()
    r = api.post("/api/v1/parcels", headers={**auth_header(jp), **key}, json=body)
    again = api.post("/api/v1/parcels", headers={**auth_header(jp), **key}, json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    assert again.json()["id"] == p["id"]  # double tap: one parcel
    assert (p["units"], p["in_transit"], p["status"]) == (10, 10, "shipped")
    assert p["tracking_url"] == "https://www.fedex.com/fedextrack/?trknbr=7712 3456"
    assert p["eta"] == "2026-10-29"  # ship date + planned lead time (9 days)
    assert p["overdue_days"] > 0      # it is past its ETA and not received
    sa, sb = skus_by_code(api, jp)["LG-A"]["id"], skus_by_code(api, jp)["LG-B"]["id"]
    assert (pos(db, sa)["in_transit"], pos(db, sa)["us_available"]) == (6, 0)  # in transit is not sellable
    over = api.post("/api/v1/parcels", headers={**auth_header(jp), **idem()},
                    json={"lines": [{"purchase_line_id": a["id"], "qty": 5}]})
    assert over.status_code == 409  # only 4 of line A left to ship

    la, lb = {l["sku_code"]: l for l in p["lines"]}["LG-A"], {l["sku_code"]: l for l in p["lines"]}["LG-B"]
    url = f"/api/v1/parcels/{p['id']}/receive"
    assert api.post(url, headers={**auth_header(jp), **idem()},
                    json={"lines": [{"shipment_line_id": la["id"], "received": 6}]}).status_code == 403
    no_reason = api.post(url, headers={**auth_header(us), **idem()},
                         json={"lines": [{"shipment_line_id": lb["id"], "received": 2, "lost": 1}]})
    assert no_reason.status_code == 422
    too_many = api.post(url, headers={**auth_header(us), **idem()},
                        json={"lines": [{"shipment_line_id": lb["id"], "received": 5}]})
    assert too_many.status_code == 409
    k2 = idem()
    body2 = {"received_on": "2026-10-30", "lines": [
        {"shipment_line_id": la["id"], "received": 6},
        {"shipment_line_id": lb["id"], "received": 2, "lost": 1, "lost_reason": "box crushed, card bent"}]}
    r2 = api.post(url, headers={**auth_header(us), **k2}, json=body2)
    api.post(url, headers={**auth_header(us), **k2}, json=body2)  # same submit twice: once
    assert r2.status_code == 200, r2.text
    assert (r2.json()["status"], r2.json()["in_transit"], r2.json()["lost"]) == ("shipped", 1, 1)
    assert (pos(db, sa)["us_available"], pos(db, sb)["us_available"], pos(db, sb)["in_transit"]) == (6, 2, 1)
    loss = db.execute(text("""SELECT amount_base, memo FROM financial_entries WHERE entry_type = 'inventory_adjustment'
                              AND sku_id = :s"""), {"s": sb}).one()
    assert loss.amount_base == Decimal("-20.0000") and "box crushed" in loss.memo  # 1 x (3000 JPY / 150)
    # the last box arrives
    r3 = api.post(url, headers={**auth_header(us), **idem()},
                  json={"received_on": "2026-11-02", "lines": [{"shipment_line_id": lb["id"], "received": 1}]})
    d = r3.json()
    assert (d["status"], d["in_transit"], d["overdue_days"]) == ("received", 0, 0)
    assert [e["event"] for e in d["events"]] == ["shipped", "partially_received", "received"]
    lt = api.get("/api/v1/logistics/lead-time", headers=auth_header(us), params={"days": 3650}).json()
    assert lt["parcels"] == 1 and lt["average_days"] == 13.0  # 10/20 -> 11/02
    assert api.get("/api/v1/parcels", headers=auth_header(us)).json() == []          # no open parcels left
    assert len(api.get("/api/v1/parcels", headers=auth_header(us), params={"status": "received"}).json()) == 1
    assert db.execute(text("SELECT count(*) FROM v_inventory_reconciliation")).scalar_one() == 0


def test_update_tracking_and_eta(api, db):
    jp, lines = batch_lines(api, db)
    p = api.post("/api/v1/parcels", headers={**auth_header(jp), **idem()},
                 json={"lines": [{"purchase_line_id": lines["LG-A"]["id"], "qty": 1}]}).json()
    assert p["tracking_url"] is None
    r = api.patch(f"/api/v1/parcels/{p['id']}", headers=auth_header(jp),
                  json={"carrier_id": carrier(db, "日本郵便 EMS"), "tracking_no": "EM123456789JP", "eta": "2099-01-01"})
    assert r.status_code == 200 and "EM123456789JP" in r.json()["tracking_url"] and r.json()["overdue_days"] == 0
    _, ro = token_for(api, db, "read_only")
    assert api.patch(f"/api/v1/parcels/{p['id']}", headers=auth_header(ro), json={}).status_code == 403


def test_damage_and_count_differences_hit_profit(api, db):
    from tests.test_catalog import EDITOR
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("SHR-1",))["sku_ids"][0]
    us_stock(api, tok, sku, 10, "5")  # opening stock is not a gain
    r = api.post("/api/v1/inventory/adjustments", headers={**auth_header(tok), **idem()},
                 json={"location_code": "US_MAIN", "kind": "damage", "reason": "water damage",
                       "lines": [{"sku_id": sku, "qty": 2}]})
    assert r.status_code == 201, r.text
    rows = db.execute(text("SELECT amount_base FROM financial_entries WHERE entry_type = 'inventory_adjustment' "
                           "AND sku_id = :s ORDER BY id"), {"s": sku}).scalars().all()
    assert rows == [Decimal("-10.0000")]
    r = api.post("/api/v1/inventory/adjustments", headers={**auth_header(tok), **idem()},
                 json={"location_code": "US_MAIN", "kind": "increase", "reason": "found in back room",
                       "lines": [{"sku_id": sku, "qty": 1, "unit_cost_usd": "5"}]})
    assert r.status_code == 201, r.text
    total = db.execute(text("SELECT sum(amount_base) FROM financial_entries WHERE entry_type = 'inventory_adjustment' "
                            "AND sku_id = :s"), {"s": sku}).scalar_one()
    assert total == Decimal("-5.0000")
    db.commit()
    from tests.test_users_rbac import admin_token
    _, admin = admin_token(api, db)
    from datetime import date
    today = date.today().isoformat()
    pnl = api.get("/api/v1/reports/pnl", headers=auth_header(admin),
                  params={"from": today, "to": today, "granularity": "total"}).json()["total"]
    assert pnl["shrinkage"] == -5.0 and pnl["gross_profit"] == -5.0


def test_outbound_backlog(api, db):
    from tests.test_catalog import EDITOR
    _, tok = token_for(api, db, *EDITOR)
    create_product(api, tok, codes=("OB-1",))
    sku = skus_by_code(api, tok)["OB-1"]["id"]
    us_stock(api, tok, sku, 5, "3")
    P.process_order(db, W.order("OB-WAIT", created="2026-11-01T20:00:00Z"),
                    [W.item("OB-I1", "OB-WAIT", ext="OB-1", product_id="OBP")], source="api")
    P.process_order(db, W.order("OB-DONE", "completed", created="2026-11-01T20:00:00Z"),
                    [W.item("OB-I2", "OB-DONE", ext="OB-1", product_id="OBP")], source="api")
    db.commit()
    _, ops = token_for(api, db, "us_live_ops")
    r = api.get("/api/v1/logistics/outbound-backlog", headers=auth_header(ops)).json()
    ids = [x["external_order_id"] for x in r["items"]]
    assert ids == ["OB-WAIT"] and r["late_orders"] == 1 and r["units"] == 1


def test_old_shipment_of_unmapped_order_is_rechecked(api, db):
    """Found on real data: Whatnot keeps the order 'pending_fulfillment' while its parcel is already in transit;
    for a line not mapped to a SKU the shipment must still be re-read, or the order looks unshipped forever."""
    from app.integrations.whatnot import sync
    fake = W.FakeWhatnot()
    old = "2026-10-21T09:30:00Z"
    o = W.order("UNM-1", created=old)
    fake.add_order(o, [W.item("UNM-I", "UNM-1", title="Unmapped promo #3", product_id="NOPE", shipment_id="SH-OLD")])
    P.process_order(db, o, fake.items["UNM-1"], source="api")
    sh = W.shipment("SH-OLD", status="in_transit")
    sh["created_at"] = old  # outside the sync window
    fake.shipments["SH-OLD"] = sh
    fake.shipment_items["SH-OLD"] = [W.shipment_item("X1", "SH-OLD", "UNM-1", "UNM-I")]
    db.commit()
    _, ops = token_for(api, db, "us_live_ops")
    assert "UNM-1" in [x["external_order_id"] for x in api.get("/api/v1/logistics/outbound-backlog", headers=auth_header(ops)).json()["items"]]
    with fake.client() as c:
        sync.run(db, c, days=3, kinds=("shipments",))
    assert "UNM-1" not in [x["external_order_id"] for x in api.get("/api/v1/logistics/outbound-backlog", headers=auth_header(ops)).json()["items"]]
