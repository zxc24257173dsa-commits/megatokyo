"""Phase 8: sales trends - sold units by period, stock demand (bundles, box openings), movers, weeks of cover."""
from datetime import UTC, date, datetime, timedelta

import pytest

from app.integrations.whatnot import processing as P
from app.services import ledger, trends as T
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product, skus_by_code
from tests.test_whatnot import us_stock


def iso(d: date, hour: int = 20) -> str:
    """A UTC timestamp that falls on business date `d` (20:00 UTC = 15:00 or 16:00 New York)."""
    return f"{d.isoformat()}T{hour:02d}:00:00Z"


def sell(db, oid, day, qty=1, price=10, *, ext=None, title="Promo #1", status="pending_fulfillment"):
    P.process_order(db, W.order(oid, status, subtotal=price * qty, created=iso(day)),
                    [W.item(f"{oid}-I", oid, qty=qty, price=price, ext=ext, title=title, product_id=f"P-{ext or title}")],
                    source="api")


@pytest.fixture
def skus(api, db):
    _, tok = token_for(api, db, *EDITOR)
    create_product(api, tok, codes=("TR-CARD", "TR-BOX"))
    by = skus_by_code(api, tok)
    card, box = by["TR-CARD"]["id"], by["TR-BOX"]["id"]
    us_stock(api, tok, card, 20, "2")
    us_stock(api, tok, box, 10, "40")
    return tok, card, box


def test_classify():
    assert T.classify(0, 0) == ("none", None)
    assert T.classify(5, 0) == ("new", None)
    assert T.classify(0, 5) == ("stopped", -100.0)
    assert T.classify(12, 10) == ("flat", 20.0)
    assert T.classify(20, 10) == ("up", 100.0)
    assert T.classify(5, 10) == ("down", -50.0)
    assert T.classify(11, 10)[0] == "flat"
    assert T.classify(2, 1)[0] == "low"


def test_overview_weeks_only_paid_orders(db, skus):
    _, card, _ = skus
    mon, sun = date(2026, 11, 2), date(2026, 11, 8)
    sell(db, "T-1", mon, qty=2, ext="TR-CARD")
    sell(db, "T-2", sun, qty=1, ext="TR-CARD")
    sell(db, "T-3", sun + timedelta(days=1), qty=4, ext="TR-CARD")         # next Monday -> next week
    sell(db, "T-G", mon, qty=1, price=0, ext="TR-CARD")                     # giveaway: a unit, no sales
    sell(db, "T-U", mon, qty=1, title="Mystery card #7")                    # unmapped
    sell(db, "T-U2", mon, qty=1, title="Mystery card #8")
    sell(db, "T-X", mon, qty=9, ext="TR-CARD", status="pending")            # unpaid: never counts
    sell(db, "T-C", mon, qty=9, ext="TR-CARD", status="cancelled")
    o = T.overview(db, mon, date(2026, 11, 15), "week")
    w1, w2 = o["columns"]
    assert (w1["label"], w1["units"], w1["giveaway_units"], w1["orders"], w1["sales"]) == ("11/02–11/08", 6, 1, 5, 50.0)
    assert (w2["units"], w2["sales"]) == (4, 40.0)
    by = {i["label"]: i for i in o["items"]}
    assert by["Mystery card"]["total_units"] == 2 and not by["Mystery card"]["mapped"]  # "#7" and "#8" -> one row
    assert by["TR-CARD Pokemon Card Box"]["units"] == [4, 4]
    d = T.overview(db, mon, mon + timedelta(days=2), "day")
    assert [c["units"] for c in d["columns"]] == [5, 0, 0]


def test_demand_expands_bundles_and_counts_box_openings(api, db, skus):
    tok, card, box = skus
    r = api.post("/api/v1/bundles", headers=auth_header(tok), json={
        "sku_code": "TR-SET", "name": "set", "components": [{"sku_id": box, "qty": 1}, {"sku_id": card, "qty": 3}]})
    assert r.status_code == 201
    today = ledger.biz_date(datetime.now(UTC))
    sell(db, "D-1", today, qty=2, ext="TR-SET")          # 2 sets -> 2 boxes + 6 cards of demand
    sell(db, "D-2", today, qty=1, ext="TR-CARD")
    d = api.get(f"/api/v1/skus/{box}", headers=auth_header(tok)).json()
    api.put(f"/api/v1/skus/{box}/box-contents", headers=auth_header(tok),
            json={"version": d["version"], "contents": [{"sku_id": card, "qty": 5}]})
    assert api.post("/api/v1/inventory/box-openings", headers={**auth_header(tok), **idem()},
                    json={"box_sku_id": box, "location_code": "US_MAIN", "qty": 3}).status_code == 201
    dd = T.daily_demand(db, today, today)
    assert dd[card][today] == 7 and dd[box][today] == 5    # cards: 6 via sets + 1 ; boxes: 2 via sets + 3 opened
    s = T.sku_demand(db, card, days=7)
    assert s["total"] == 7 and s["days_with_demand"] == 1 and s["mean_per_day"] == 1.0
    assert s["std_per_day"] == pytest.approx(2.6458, abs=1e-3)


def test_movers_trend_and_weeks_of_cover(api, db, skus):
    tok, card, box = skus
    today = ledger.biz_date(datetime.now(UTC))
    for i in range(10):                                   # 10 sold in the previous 4 weeks
        sell(db, f"M-P{i}", today - timedelta(days=30 + i % 5), ext="TR-CARD")
    for i in range(4):                                    # 4 x 2 = 8 sold in the last 4 weeks
        sell(db, f"M-R{i}", today - timedelta(days=i), qty=2, ext="TR-CARD")
    m = T.movers(db, weeks=4)
    row = next(r for r in m["skus"] if r["sku_id"] == card)
    assert (row["sold_recent"], row["sold_prev"], row["trend"]) == (8, 10, "flat")
    assert row["weekly_demand"] == 2.0 and row["us_available"] == 2 and row["weeks_of_cover"] == 1.0  # 20 - 18 reserved
    assert sum(row["weekly_spark"]) == 18
    boxrow = next(r for r in m["skus"] if r["sku_id"] == box)
    assert boxrow["trend"] == "none" and boxrow["weeks_of_cover"] is None


def test_money_only_for_finance_roles(api, db, skus):
    sell(db, "MN-1", date(2026, 11, 3), ext="TR-CARD")
    db.commit()
    _, inv = token_for(api, db, "jp_inventory")           # sees orders, not the books
    r = api.get("/api/v1/reports/trends", headers=auth_header(inv), params={"from": "2026-11-01", "to": "2026-11-08"})
    cols = r.json()["columns"]  # 11/01 is a Sunday: the first week is clipped to that one day
    assert r.status_code == 200 and all(c["sales"] is None for c in cols) and sum(c["units"] for c in cols) == 1
    m = api.get("/api/v1/reports/trends/movers", headers=auth_header(inv)).json()
    assert all(x["sales_recent"] is None for x in m["skus"])
    _, ops = token_for(api, db, "us_live_ops")
    assert api.get("/api/v1/reports/trends", headers=auth_header(ops),
                   params={"from": "2026-11-02", "to": "2026-11-08"}).json()["columns"][0]["sales"] == 10.0
    assert api.get("/api/v1/reports/trends", headers=auth_header(ops),
                   params={"from": "2026-01-01", "to": "2026-11-08", "granularity": "day"}).status_code == 422
