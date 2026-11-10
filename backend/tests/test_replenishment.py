"""Phase 10: replenishment suggestions (advisory only; direct ship Japan -> US)."""
import math
import statistics
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.integrations.whatnot import processing as P
from app.services import ledger, replenishment as R
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product, skus_by_code
from tests.test_whatnot import us_stock


def today():
    return ledger.biz_date(datetime.now(UTC))


def sell_on(db, sku_code, day, qty, n):
    P.process_order(db, W.order(f"RP-{sku_code}-{n}", subtotal=10 * qty, created=f"{day.isoformat()}T20:00:00Z"),
                    [W.item(f"RP-{sku_code}-{n}-I", f"RP-{sku_code}-{n}", qty=qty, price=10, ext=sku_code,
                            product_id=f"P-{sku_code}")], source="api")


def setup_skus(api, db, codes):
    _, tok = token_for(api, db, *EDITOR)
    create_product(api, tok, codes=codes)
    return tok, {c: s["id"] for c, s in skus_by_code(api, tok).items()}


def row(data, sku):
    return next(r for r in data["suggestions"] if r["sku_id"] == sku)


def test_formula_step_by_step(api, db):
    tok, ids = setup_skus(api, db, ("RP-A",))
    us_stock(api, tok, ids["RP-A"], 200, "4")
    as_of = today() - timedelta(days=1)
    pattern = [2, 0, 4, 1, 3, 0, 2] * 5  # 35 days, newest last
    n = 0
    for i, q in enumerate(pattern[-30:]):
        if q:
            sell_on(db, "RP-A", as_of - timedelta(days=29 - i), q, n)
            n += 1
    sell_on(db, "RP-A", today(), 50, 999)  # today's sales are not used for velocity (day not complete) ...
    d = R.compute(db)
    r = row(d, ids["RP-A"])
    s = pattern[-30:]
    v7, v14, v30 = sum(s[-7:]) / 7, sum(s[-14:]) / 14, sum(s) / 30
    v = 0.5 * v7 + 0.3 * v14 + 0.2 * v30
    sigma = statistics.stdev(s)
    ss = math.ceil(1.65 * sigma * math.sqrt(11))
    assert (r["v7"], r["v14"], r["v30"]) == (round(v7, 4), round(v14, 4), round(v30, 4))
    assert r["v_blended"] == round(v, 4) and r["sigma_daily"] == round(sigma, 4) and r["safety_stock"] == ss
    target = v * 18 + ss
    paid = sum(s)  # ... but they are reserved, so US available already excludes them
    avail = 200 - paid - 50
    assert r["us_available"] == avail and r["position"] == avail
    assert r["target"] == round(target, 2)
    assert r["action"] == "none" and r["suggested_purchase_qty"] == 0 and r["status_color"] == "green"


def test_buy_suggestion_counts_transit_and_open_purchases_and_case_pack(api, db):
    tok, ids = setup_skus(api, db, ("RP-B",))
    sku = ids["RP-B"]
    us_stock(api, tok, sku, 3, "4")
    as_of = today() - timedelta(days=1)
    for i in range(30):
        sell_on(db, "RP-B", as_of - timedelta(days=i), 1, i)  # 1 a day, steady: sigma 0
    us_stock(api, tok, sku, 30, "4")  # restock after the sales: 33 - 30 = 3 available
    db.execute(text("INSERT INTO sku_replenishment_params (sku_id, case_pack, min_ship_qty) VALUES (:s, 6, 0)"),
               {"s": sku})
    r = row(R.compute(db), sku)
    assert (r["v_blended"], r["safety_stock"], r["us_available"]) == (1.0, 0, 3)
    assert r["target"] == 18.0 and r["action"] == "buy"
    assert r["suggested_purchase_qty"] == 18  # 18 - 3 = 15 -> case pack 6 -> 18
    assert r["status_color"] == "red"            # 3 days of stock, 11 days to get more
    assert r["latest_order_date"] == today() + timedelta(days=3 - 11)


def test_box_contents_open_boxes_and_drive_box_demand(api, db):
    tok, ids = setup_skus(api, db, ("RP-BOX", "RP-CARD"))
    box, card = ids["RP-BOX"], ids["RP-CARD"]
    d = api.get(f"/api/v1/skus/{box}", headers=auth_header(tok)).json()
    api.put(f"/api/v1/skus/{box}/box-contents", headers=auth_header(tok),
            json={"version": d["version"], "contents": [{"sku_id": card, "qty": 4}]})
    us_stock(api, tok, box, 5, "40")
    us_stock(api, tok, card, 40, "10")
    as_of = today() - timedelta(days=1)
    for i in range(30):
        sell_on(db, "RP-CARD", as_of - timedelta(days=i), 1, i)   # 1 card a day -> 0.25 box a day
    # opening boxes in the past is not box demand again
    assert api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **idem()},
                    json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 1}).status_code == 201
    data = R.compute(db)
    c, b = row(data, card), row(data, box)
    assert c["action"] in ("open_box", "none") and c["suggested_purchase_qty"] == 0  # cards are never bought
    assert c["source_box_sku_id"] == box
    assert b["v_blended"] == 0.25 and any("RP-CARD" in n for n in b["notes"])
    if c["action"] == "open_box":
        assert c["boxes_to_open"] == math.ceil((c["target"] - c["position"]) / 4)


def test_bundles_excluded_and_new_sku_velocity(api, db):
    tok, ids = setup_skus(api, db, ("RP-N",))
    sku = ids["RP-N"]
    r = api.post("/api/v1/bundles", headers=auth_header(tok), json={
        "sku_code": "RP-SET", "name": "set", "components": [{"sku_id": sku, "qty": 2}]})
    bundle = r.json()["id"]
    _, inv = token_for(api, db, "jp_inventory")
    p = api.put(f"/api/v1/replenishment/skus/{sku}", headers=auth_header(inv), json={"manual_daily_velocity": "2"})
    assert p.status_code == 200, p.text
    data = R.compute(db)
    assert all(x["sku_id"] != bundle for x in data["suggestions"])
    n = row(data, sku)
    assert n["v_blended"] == 2.0 and n["action"] == "buy" and n["suggested_purchase_qty"] == 36  # 2 x 18, no stock
    api.put(f"/api/v1/replenishment/skus/{sku}", headers=auth_header(inv), json={"excluded": True})
    assert all(x["sku_id"] != sku for x in R.compute(db)["suggestions"])


def test_runs_override_confirm_export_and_permissions(api, db):
    tok, ids = setup_skus(api, db, ("RP-R",))
    db.execute(text("INSERT INTO sku_replenishment_params (sku_id, manual_daily_velocity) VALUES (:s, 1)"),
               {"s": ids["RP-R"]})
    db.commit()
    _, ro = token_for(api, db, "read_only")
    _, inv = token_for(api, db, "jp_inventory")
    assert api.post("/api/v1/replenishment/runs", headers=auth_header(ro), json={}).status_code == 403
    r = api.post("/api/v1/replenishment/runs", headers=auth_header(inv), json={"note": "weekly check"})
    assert r.status_code == 201, r.text
    run = r.json()
    s = next(x for x in run["suggestions"] if x["sku_id"] == ids["RP-R"])
    assert s["suggested_purchase_qty"] == 18 and s["final_qty"] == 18
    assert api.get("/api/v1/replenishment/runs/latest", headers=auth_header(ro)).json()["run"]["id"] == run["run"]["id"]
    url = f"/api/v1/replenishment/suggestions/{s['id']}"
    assert api.patch(url, headers=auth_header(inv), json={"qty": 24}).status_code == 422  # reason required
    assert api.patch(url, headers=auth_header(inv), json={"qty": 24, "reason": "supplier sells by 12"}).status_code == 200
    c = api.post(f"/api/v1/replenishment/runs/{run['run']['id']}/confirm", headers=auth_header(inv),
                 json={"suggestion_ids": [s["id"]]})
    assert c.json()["confirmed"] == 1
    assert api.patch(url, headers=auth_header(inv), json={"qty": 1, "reason": "x?"}).status_code == 409
    csv_ = api.get(f"/api/v1/replenishment/runs/{run['run']['id']}/purchase-list.csv", headers=auth_header(ro))
    body = csv_.content.decode("utf-8-sig")
    assert "RP-R" in body and ",24,18,supplier sells by 12," in body and body.strip().endswith("yes")
    # nothing was ordered: no purchase batches appeared
    assert db.execute(text("SELECT count(*) FROM purchase_batches")).scalar_one() == 0


def test_settings(api, db):
    _, inv = token_for(api, db, "jp_inventory")
    bad = api.put("/api/v1/replenishment/settings", headers=auth_header(inv), json={"weights": [0.5, 0.5, 0.5]})
    assert bad.status_code == 422
    ok = api.put("/api/v1/replenishment/settings", headers=auth_header(inv),
                 json={"supplier_lead_time_days": 10, "service_level_z": "2.05", "weights": [0.6, 0.3, 0.1]})
    assert ok.status_code == 200 and ok.json()["supplier_lead_time_days"] == 10 and ok.json()["service_level_z"] == 2.05
