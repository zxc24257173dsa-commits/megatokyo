from sqlalchemy import text

from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product


def opening(api, tok, sku, qty, cost="10", loc="JP_MAIN", headers=None):
    return api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **(headers or idem())}, json={
        "location_code": loc, "lines": [{"sku_id": sku, "qty": qty, "unit_cost_usd": cost}]})


def adjust(api, tok, sku, qty, kind, loc="JP_MAIN", **line):
    return api.post("/api/v1/inventory/adjustments", headers={**auth_header(tok), **idem()}, json={
        "location_code": loc, "kind": kind, "reason": "test", "lines": [{"sku_id": sku, "qty": qty, **line}]})


def pos(api, tok, sku):
    return api.get(f"/api/v1/skus/{sku}", headers=auth_header(tok)).json()


def test_warehouse_permissions(api, db):
    _, editor = token_for(api, db, *EDITOR)
    sku = create_product(api, editor, codes=("PERM-1",))["sku_ids"][0]
    _, us = token_for(api, db, "us_live_ops")
    _, jp = token_for(api, db, "jp_inventory")
    assert opening(api, us, sku, 5, loc="JP_MAIN").status_code == 403
    assert opening(api, jp, sku, 5, loc="US_MAIN").status_code == 403
    assert opening(api, jp, sku, 5, loc="JP_MAIN").status_code == 201
    assert opening(api, us, sku, 3, loc="US_MAIN").status_code == 201
    r = opening(api, jp, sku, 1, loc="IN_TRANSIT")
    assert r.status_code == 422  # in-transit only changes through shipments
    p = pos(api, editor, sku)
    assert (p["jp_on_hand"], p["us_on_hand"], p["in_transit"]) == (5, 3, 0)


def test_opening_stock_requires_cost_and_idempotency_key(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("OB-1",))["sku_ids"][0]
    r = api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()},
                 json={"location_code": "JP_MAIN", "lines": [{"sku_id": sku, "qty": 1}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unit_cost_required"
    r = api.post("/api/v1/inventory/opening-stock", headers=auth_header(tok),
                 json={"location_code": "JP_MAIN", "lines": [{"sku_id": sku, "qty": 1, "unit_cost_usd": "1"}]})
    assert r.status_code == 422  # missing Idempotency-Key header


def test_double_submit_does_not_double_stock(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("IDEM-1",))["sku_ids"][0]
    key = idem()
    first = opening(api, tok, sku, 10, headers=key)
    second = opening(api, tok, sku, 10, headers=key)  # e.g. double tap on a phone
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert pos(api, tok, sku)["jp_on_hand"] == 10


def test_decrease_takes_oldest_lot_first(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("FIFO-1",))["sku_ids"][0]
    opening(api, tok, sku, 10, cost="5")
    opening(api, tok, sku, 10, cost="8")
    r = adjust(api, tok, sku, 15, "decrease")
    assert r.status_code == 201 and len(r.json()["movement_ids"]) == 2  # split across two lots
    lots = pos(api, tok, sku)["lots"]
    assert [(l["unit_cost_base"], l["jp"]) for l in lots] == [("5.000000", 0), ("8.000000", 5)]


def test_damage_goes_to_loss_and_insufficient_stock_is_rejected(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("DMG-1",))["sku_ids"][0]
    opening(api, tok, sku, 3, loc="US_MAIN")
    assert adjust(api, tok, sku, 1, "damage", loc="US_MAIN").status_code == 201
    r = adjust(api, tok, sku, 5, "damage", loc="US_MAIN")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "insufficient_stock" and err["details"]["available"] == 2
    assert pos(api, tok, sku)["us_on_hand"] == 2
    to_loss = db.execute(text("""SELECT count(*) FROM inventory_movements m JOIN locations l ON l.id = m.to_location_id
                                 WHERE m.sku_id = :s AND l.code = 'LOSS'"""), {"s": sku}).scalar_one()
    assert to_loss == 1


def test_increase_uses_newest_lot_or_requires_cost(api, db):
    _, tok = token_for(api, db, *EDITOR)
    new_sku, old_sku = create_product(api, tok, codes=("INC-NEW", "INC-OLD"))["sku_ids"]
    r = adjust(api, tok, new_sku, 2, "increase")
    assert r.status_code == 422 and r.json()["error"]["code"] == "unit_cost_required"
    assert adjust(api, tok, new_sku, 2, "increase", unit_cost_usd="4").status_code == 201
    opening(api, tok, old_sku, 1, cost="5")
    opening(api, tok, old_sku, 1, cost="9")
    assert adjust(api, tok, old_sku, 3, "increase").status_code == 201
    lots = pos(api, tok, old_sku)["lots"]
    assert [l["jp"] for l in lots] == [1, 4]  # found units joined the newest lot


def test_bundles_cannot_be_adjusted(api, db):
    _, tok = token_for(api, db, *EDITOR)
    comp = create_product(api, tok, codes=("BC-1",))["sku_ids"][0]
    bundle = api.post("/api/v1/bundles", headers=auth_header(tok), json={
        "sku_code": "BC-SET", "name": "set", "components": [{"sku_id": comp, "qty": 1}]}).json()
    r = opening(api, tok, bundle["id"], 1)
    assert r.status_code == 422 and r.json()["error"]["code"] == "bundle_no_stock"


def test_stock_count_workflow(api, db):
    _, staff = token_for(api, db, "jp_inventory", "us_live_ops")  # can count, cannot approve
    _, manager = token_for(api, db, "tw_manager")
    _, editor = token_for(api, db, *EDITOR)
    a, b, c = create_product(api, editor, codes=("SC-A", "SC-B", "SC-C"))["sku_ids"]
    opening(api, editor, a, 10)
    opening(api, editor, b, 4)

    sc = api.post("/api/v1/inventory/stock-counts", headers=auth_header(staff),
                  json={"location_code": "JP_MAIN", "sku_ids": [a, b, c]}).json()
    assert {l["sku_code"]: l["expected_qty"] for l in sc["lines"]} == {"SC-A": 10, "SC-B": 4, "SC-C": 0}
    api.put(f"/api/v1/inventory/stock-counts/{sc['id']}/counts", headers=auth_header(staff),
            json={"counts": [{"sku_id": a, "counted_qty": 8}, {"sku_id": b, "counted_qty": 4}]})
    r = api.post(f"/api/v1/inventory/stock-counts/{sc['id']}/submit", headers=auth_header(staff))
    assert r.status_code == 422 and r.json()["error"]["code"] == "count_incomplete"
    api.put(f"/api/v1/inventory/stock-counts/{sc['id']}/counts", headers=auth_header(staff),
            json={"counts": [{"sku_id": c, "counted_qty": 0}]})
    assert api.post(f"/api/v1/inventory/stock-counts/{sc['id']}/submit", headers=auth_header(staff)).status_code == 200

    assert api.post(f"/api/v1/inventory/stock-counts/{sc['id']}/approve", headers=auth_header(staff)).status_code == 403
    r = api.post(f"/api/v1/inventory/stock-counts/{sc['id']}/approve", headers=auth_header(manager))
    assert r.status_code == 200 and r.json()["status"] == "approved"
    diffs = {l["sku_code"]: l["difference_qty"] for l in r.json()["lines"]}
    assert diffs == {"SC-A": -2, "SC-B": 0, "SC-C": 0}
    assert pos(api, editor, a)["jp_on_hand"] == 8

    again = api.put(f"/api/v1/inventory/stock-counts/{sc['id']}/counts", headers=auth_header(staff),
                    json={"counts": [{"sku_id": a, "counted_qty": 1}]})
    assert again.status_code == 409


def test_movement_history_and_summary(api, db):
    user, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("HIST-1",))["sku_ids"][0]
    opening(api, tok, sku, 6, cost="2")
    adjust(api, tok, sku, 1, "damage")
    hist = api.get("/api/v1/inventory/movements", headers=auth_header(tok), params={"sku_id": sku, "limit": 1}).json()
    assert len(hist["items"]) == 1 and hist["next_before_id"] is not None
    assert hist["items"][0]["movement_type"] == "damage" and hist["items"][0]["created_by"] == "Test User"
    summary = {l["location_code"]: l for l in api.get("/api/v1/inventory/summary", headers=auth_header(tok)).json()["locations"]}
    assert summary["JP_MAIN"]["units"] >= 5
    assert "value_usd" in summary["JP_MAIN"]  # tw_manager has finance.view
    assert db.execute(text("SELECT count(*) FROM v_inventory_reconciliation")).scalar_one() == 0
