"""Phase 7: operating expenses, P&L by week / month, accounting period close, ledger export."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.integrations.whatnot import financial as F
from app.integrations.whatnot import processing as P
from tests import whatnot_fake as W
from tests.conftest import auth_header, token_for
from tests.test_whatnot import setup, us_stock  # noqa: F401  (fixture)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
SHOW = "4adc04df-0000-4000-8000-000000000001"


def idem():
    return {"Idempotency-Key": uuid.uuid4().hex}


def add_expense(api, tok, *, headers=None, files=None, **form):
    data = {"expense_date": "2026-11-05", "category": "packaging", "amount": "20", "currency": "USD", **form}
    return api.post("/api/v1/expenses", headers={**auth_header(tok), **(headers or idem())}, data=data, files=files)


def test_expense_in_yen_with_receipt_once(api, db):
    _, tok = token_for(api, db, "tw_manager")
    key = idem()
    r = add_expense(api, tok, headers=key, amount="15000", currency="JPY", fx_quote="150", category="rent",
                    vendor="Tokyo office", files={"receipt": ("r.png", PNG, "image/png")})
    again = add_expense(api, tok, headers=key, amount="15000", currency="JPY", fx_quote="150", category="rent")
    assert r.status_code == 201, r.text
    e = r.json()
    assert again.json()["id"] == e["id"]  # double tap saved once
    assert e["amount_usd"] == 100.0 and e["fx_quote"] == 150.0 and e["receipt_url"].startswith("/api/v1/files/")
    led = db.execute(text("SELECT entry_type, amount_base, currency_orig FROM financial_entries "
                          "WHERE source_ref = :r"), {"r": f"expense:{e['id']}"}).all()
    assert led == [("operating_expense", Decimal("-100.0000"), "JPY")]
    assert add_expense(api, tok, currency="JPY").status_code == 422  # rate is required for yen
    pdf = add_expense(api, tok, files={"receipt": ("r.pdf", b"%PDF-1.7 test", "application/pdf")})
    assert pdf.status_code == 201
    bad = add_expense(api, tok, files={"receipt": ("x.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")})
    assert bad.status_code == 415
    assert add_expense(api, tok, expense_date="2099-01-01").status_code == 422
    assert add_expense(api, tok, category="nope").status_code == 422


def test_who_sees_and_voids_expenses(api, db):
    _, ops = token_for(api, db, "us_live_ops")       # can record, cannot see the books
    _, mgr = token_for(api, db, "tw_manager")        # finance.view
    _, ro = token_for(api, db, "read_only")
    mine = add_expense(api, ops, description="tape").json()
    other = add_expense(api, mgr, category="payroll", amount="900").json()
    seen = {x["id"] for x in api.get("/api/v1/expenses", headers=auth_header(ops)).json()}
    assert mine["id"] in seen and other["id"] not in seen  # salaries stay private
    assert {mine["id"], other["id"]} <= {x["id"] for x in api.get("/api/v1/expenses", headers=auth_header(mgr)).json()}
    assert api.get("/api/v1/expenses", headers=auth_header(ro)).status_code == 403
    assert api.post(f"/api/v1/expenses/{other['id']}/void", headers=auth_header(ops), json={"reason": "x?"}).status_code == 404
    v = api.post(f"/api/v1/expenses/{mine['id']}/void", headers=auth_header(ops), json={"reason": "typo"})
    assert v.status_code == 200 and v.json()["voided_at"]
    assert api.post(f"/api/v1/expenses/{mine['id']}/void", headers=auth_header(ops), json={"reason": "again"}).status_code == 409
    net = db.execute(text("SELECT sum(amount_base) FROM financial_entries WHERE source_ref = :r"),
                     {"r": f"expense:{mine['id']}"}).scalar_one()
    assert net == 0  # booked and reversed, both kept
    with pytest.raises(DBAPIError, match="ERP_FROZEN"), db.begin_nested():
        db.execute(text("UPDATE operating_expenses SET amount_base = 1 WHERE id = :i"), {"i": other["id"]})
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"), db.begin_nested():
        db.execute(text("DELETE FROM operating_expenses WHERE id = :i"), {"i": other["id"]})


def _sell(db, oid, price, *, created="2026-11-03T20:00:00Z", status="completed", show=None, fees=True):
    P.process_order(db, W.order(oid, status, subtotal=price, created=created, show_id=show),
                    [W.item(f"{oid}-I", oid, price=price, ext="WN-BOX")], source="api")
    if fees:
        F.ingest_rows(db, [W.earnings(f"L-{oid}", oid, price, commission=round(price * 0.08, 2), proc=1.00,
                                      completed=created.replace("T", " ").rstrip("Z"))], source="api")


def test_pnl_by_month_and_week(api, db, setup):
    tok, _ = setup  # 5 boxes at $40 in the US
    _sell(db, "PL-OCT", 100, created="2026-10-28T20:00:00Z")   # Wed 10/28
    _sell(db, "PL-NOV", 50, created="2026-11-03T20:00:00Z")    # Tue 11/03
    _sell(db, "PL-OPEN", 60, created="2026-11-04T20:00:00Z", status="pending_fulfillment", fees=False)
    db.commit()
    _, mgr = token_for(api, db, "tw_manager")
    add_expense(api, mgr, expense_date="2026-11-02", category="rent", amount="30")
    add_expense(api, mgr, expense_date="2026-10-30", category="packaging", amount="5")

    r = api.get("/api/v1/reports/pnl", headers=auth_header(mgr),
                params={"from": "2026-10-01", "to": "2026-11-30", "granularity": "month"})
    assert r.status_code == 200, r.text
    d = r.json()
    oct_, nov = d["columns"]
    assert oct_["label"] == "2026-10" and oct_["sales"] == 100 and oct_["commission"] == -8 and oct_["processing"] == -1
    assert oct_["cogs"] == -40 and oct_["gross_profit"] == 51 and oct_["opex"] == -5 and oct_["net_profit"] == 46
    assert nov["sales"] == 110 and nov["cogs"] == -40  # the unshipped order has revenue but no cost yet
    assert nov["opex_by_category"] == {"rent": -30.0}
    t = d["total"]
    assert t["net_profit"] == round(oct_["net_profit"] + nov["net_profit"], 2)
    c = d["completeness"]
    assert c["paid_orders"] == 3 and c["orders_without_fees"] == 1 and c["units_without_cogs"] == 1 and not c["complete"]

    w = api.get("/api/v1/reports/pnl", headers=auth_header(mgr),
                params={"from": "2026-10-26", "to": "2026-11-08", "granularity": "week"}).json()
    assert [x["label"] for x in w["columns"]] == ["10/26–11/01", "11/02–11/08"]  # weeks start Monday
    assert w["columns"][0]["sales"] == 100 and w["columns"][1]["opex"] == -30

    _, ops = token_for(api, db, "us_live_ops")
    assert api.get("/api/v1/reports/pnl", headers=auth_header(ops),
                   params={"from": "2026-10-01", "to": "2026-10-31"}).status_code == 403


def test_closed_month_is_frozen_and_late_entries_go_to_today(api, db, setup):
    _sell(db, "CL-1", 100, created="2026-10-20T20:00:00Z")
    db.commit()
    _, mgr = token_for(api, db, "tw_manager")
    from tests.test_users_rbac import admin_token
    _, admin = admin_token(api, db)
    assert api.post("/api/v1/accounting-periods/2026-10/close", headers=auth_header(mgr)).status_code == 403
    assert api.post("/api/v1/accounting-periods/2026-11/close", headers=auth_header(admin)).status_code == 409
    before = api.get("/api/v1/reports/pnl", headers=auth_header(admin),
                     params={"from": "2026-10-01", "to": "2026-10-31"}).json()["total"]
    assert api.post("/api/v1/accounting-periods/2026-10/close", headers=auth_header(admin)).json()["status"] == "closed"

    r = add_expense(api, mgr, expense_date="2026-10-15")
    assert r.status_code == 409 and r.json()["error"]["code"] == "period_closed"
    # a late October order from Whatnot is still booked - in the current month, with a note
    _sell(db, "CL-2", 70, created="2026-10-25T20:00:00Z")
    row = db.execute(text("""SELECT e.entry_date, e.memo FROM financial_entries e JOIN sales_orders o
                             ON o.id = e.sales_order_id WHERE o.external_order_id = 'CL-2'
                             AND e.entry_type = 'revenue'""")).one()
    assert row.entry_date.month == 11 and "2026-10" in row.memo
    after = api.get("/api/v1/reports/pnl", headers=auth_header(admin),
                    params={"from": "2026-10-01", "to": "2026-10-31"}).json()
    assert after["total"] == before and after["columns"][0]["period_status"] == "closed"
    with pytest.raises(DBAPIError, match="ERP_PERIOD_CLOSED"), db.begin_nested():
        db.execute(text("""INSERT INTO financial_entries (entry_date, occurred_at, entry_type, amount_orig, currency_orig,
                           fx_rate, amount_base, source_type, idempotency_key)
                           VALUES ('2026-10-02', now(), 'other_income', 1, 'USD', 1, 1, 'manual', 'x-closed')"""))
    assert api.post("/api/v1/accounting-periods/2026-10/reopen", headers=auth_header(admin)).json()["status"] == "open"
    assert add_expense(api, mgr, expense_date="2026-10-15").status_code == 201


def test_show_expense_counts_in_show_net_profit(api, db, setup):
    _sell(db, "SE-1", 100, show=SHOW)
    db.commit()
    ls = db.execute(text("SELECT id FROM livestreams WHERE external_id = :s"), {"s": SHOW}).scalar_one()
    _, mgr = token_for(api, db, "tw_manager")
    add_expense(api, mgr, category="host", amount="25", livestream_id=str(ls))
    s = api.get(f"/api/v1/reports/livestreams/{ls}", headers=auth_header(mgr)).json()["summary"]
    assert s["opex"] == -25 and s["net_profit"] == round(s["gross_profit"] - 25, 2)


def test_ledger_export_csv(api, db, setup):
    _sell(db, "EX-1", 100)
    db.commit()
    _, mgr = token_for(api, db, "tw_manager")
    add_expense(api, mgr, description="=HYPERLINK(\"http://x\")", amount="3")
    r = api.get("/api/v1/reports/pnl/entries.csv", headers=auth_header(mgr), params={"from": "2026-11-01", "to": "2026-11-30"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    body = r.content.decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert lines[0].startswith("entry_date,entry_type,amount_usd")
    assert any(",revenue,100.0000," in ln and "EX-1" in ln for ln in lines)
    memo_line = next(ln for ln in lines if ",operating_expense," in ln)
    cells = next(__import__("csv").reader([memo_line]))
    assert not any(c.startswith("=") for c in cells)  # no cell can start a spreadsheet formula
