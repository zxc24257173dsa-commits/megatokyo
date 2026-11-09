"""Phase 6: Whatnot financial report -> real fees / refunds in the ledger, show analytics, per-show P&L.
Report rows follow the column set of the real seller_financial_report (verified 2026-11-09); values are made up."""
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.integrations.whatnot import financial as F
from app.integrations.whatnot import processing as P
from app.integrations.whatnot import sync
from app.models.finance import PlatformTransaction
from tests import whatnot_fake as W
from tests.conftest import auth_header, token_for
from tests.test_whatnot import setup, us_stock  # noqa: F401  (fixture)

SHOW = "14a6cef8-0000-404f-acc5-4890a92a4e56"


def by_type(db, order_ext):
    return dict(db.execute(text("""SELECT e.entry_type, sum(e.amount_base) FROM financial_entries e
                                   JOIN sales_orders o ON o.id = e.sales_order_id
                                   WHERE o.external_order_id = :o GROUP BY 1"""), {"o": order_ext}).all())


def ledger_entries(db, ledger):
    return db.execute(text("SELECT entry_type, amount_base, reversal_of_id FROM financial_entries "
                           "WHERE source_ref = :r ORDER BY id"), {"r": f"whatnot:ledger:{ledger}"}).all()


def fin_sync(db, fake):
    with fake.client() as c:
        return sync.run(db, c, days=3, kinds=("financial",))


def test_fees_booked_once_from_the_report_and_buyer_data_never_stored(db, setup):
    fake = W.FakeWhatnot()
    P.process_order(db, W.order("FO-1", subtotal=40, show_id=SHOW), [W.item("FI-1", "FO-1", price=40, ext="WN-BOX")],
                    source="api")
    fake.fin_rows = [W.earnings("L-1", "FO-1", 40, commission=3.20, ctax=0.10, proc=1.46, ptax=0.05, ship=1.39,
                                show_id=SHOW)]
    run = fin_sync(db, fake)
    assert run.status == "succeeded", run.stats
    assert run.stats["financial"]["booked"] == 1
    assert by_type(db, "FO-1") == {"revenue": Decimal("40.0000"), "platform_fee": Decimal("-3.3000"),
                                   "payment_processing_fee": Decimal("-1.5100"), "shipping_cost": Decimal("-1.3900")}
    # report link fetched without our API token
    assert fake.downloads and "authorization" not in {k.lower() for k in fake.downloads[0].headers}
    # a second sync (and the same report again) adds nothing
    fin_sync(db, fake)
    assert len(ledger_entries(db, "L-1")) == 3
    cols = {r[0] for r in db.execute(text("""SELECT column_name FROM information_schema.columns
                                             WHERE table_name = 'platform_transactions'"""))}
    assert not cols & {"buyer_name", "buyer_state", "buyer_country", "cost_of_goods"}
    dump = db.execute(text("SELECT string_agg(t::text, ' ') FROM platform_transactions t")).scalar_one()
    assert "secret_buyer" not in dump


def test_earnings_wait_for_their_order_then_book(db, setup):
    fake = W.FakeWhatnot()
    fake.fin_rows = [W.earnings("L-2", "FO-2", 25, show_id=SHOW)]
    fin_sync(db, fake)
    pt = db.scalar(select(PlatformTransaction).where(PlatformTransaction.external_id == "L-2"))
    assert pt.status == "order_not_in_erp" and ledger_entries(db, "L-2") == []
    P.process_order(db, W.order("FO-2", subtotal=25, show_id=SHOW), [W.item("FI-2", "FO-2", price=25, ext="WN-BOX")],
                    source="api")
    assert F.retry_unbooked(db) == 1
    assert {e[0] for e in ledger_entries(db, "L-2")} == {"platform_fee", "payment_processing_fee"}


def test_full_refund_returns_price_and_fees(db, setup):
    P.process_order(db, W.order("FO-3", subtotal=30), [W.item("FI-3", "FO-3", price=30, ext="WN-BOX")], source="api")
    earn = W.earnings("L-3", "FO-3", 30, commission=2.40, proc=1.17)
    rows = [earn, W.ledger_row("L-3R", "ORDER_REFUND", f"-{earn['transaction_amount']}", order_id="FO-3",
                               message="Reversal of sales transaction for order refund"),
            W.ledger_row("L-3S", "ORDER_REFUND_SHIPPING_COSTS", "-1.39", order_id="FO-3")]
    stats = F.ingest_rows(db, rows, source="api")
    assert stats["booked"] == 3
    t = by_type(db, "FO-3")
    assert t["refund"] == Decimal("-30.0000")
    assert t["platform_fee"] == 0 and t["payment_processing_fee"] == 0  # fees came back
    assert t["shipping_cost"] == Decimal("-1.3900")
    # order nets to what Whatnot actually paid out: 30 - 2.40 - 1.17 - 26.43 - 1.39
    assert sum(t.values()) == Decimal("-1.3900")


def test_partial_refund_is_booked_as_the_net_amount(db, setup):
    P.process_order(db, W.order("FO-4", subtotal=50), [W.item("FI-4", "FO-4", price=50, ext="WN-BOX")], source="api")
    F.ingest_rows(db, [W.earnings("L-4", "FO-4", 50), W.ledger_row("L-4R", "ORDER_REFUND", "-10.00", order_id="FO-4")],
                  source="api")
    assert by_type(db, "FO-4")["refund"] == Decimal("-10.0000")
    assert "platform_fee" in by_type(db, "FO-4")


def test_other_transaction_types(db, setup):
    rows = [W.ledger_row("L-5a", "SHIPPING_CHARGE", "-4.25"), W.ledger_row("L-5b", "CARRIER_ADJUSTMENT_REFUND", "2.00"),
            W.ledger_row("L-5c", "TIP", "5.00", show_id=SHOW), W.ledger_row("L-5d", "APPEALED_ORDER_REFUND", "12.00"),
            W.ledger_row("L-5e", "SOMETHING_NEW", "-1.00"), W.ledger_row("L-5f", "TIP", "-1.00")]
    stats = F.ingest_rows(db, rows, source="api")
    assert stats["booked"] == 4 and stats["needs_review"] == 2
    got = {r[0]: (r[1], r[2]) for r in db.execute(text(
        """SELECT split_part(source_ref, ':', 3), entry_type, amount_base FROM financial_entries
           WHERE source_ref LIKE 'whatnot:ledger:L-5%'"""))}
    assert got == {"L-5a": ("shipping_cost", Decimal("-4.2500")), "L-5b": ("shipping_adjustment", Decimal("2.0000")),
                   "L-5c": ("other_income", Decimal("5.0000")), "L-5d": ("refund_adjustment", Decimal("12.0000"))}
    tip = db.scalar(select(PlatformTransaction).where(PlatformTransaction.external_id == "L-5c"))
    assert tip.livestream_id is not None  # tip belongs to its show


def test_changed_amounts_are_flagged_never_applied(db, setup):
    P.process_order(db, W.order("FO-6", subtotal=20), [W.item("FI-6", "FO-6", price=20, ext="WN-BOX")], source="api")
    F.ingest_rows(db, [W.earnings("L-6", "FO-6", 20, commission=1.60)], source="api")
    F.ingest_rows(db, [W.earnings("L-6", "FO-6", 20, commission=9.99)], source="api")
    pt = db.scalar(select(PlatformTransaction).where(PlatformTransaction.external_id == "L-6"))
    assert pt.upstream_changed and pt.commission_fee == Decimal("1.6000")
    assert by_type(db, "FO-6")["platform_fee"] == Decimal("-1.6000")
    with pytest.raises(DBAPIError, match="ERP_FROZEN"), db.begin_nested():
        db.execute(text("UPDATE platform_transactions SET amount = 1 WHERE external_id = 'L-6'"))
    with pytest.raises(DBAPIError, match="ERP_APPEND_ONLY"), db.begin_nested():
        db.execute(text("DELETE FROM platform_transactions WHERE external_id = 'L-6'"))


def test_amount_mismatch_is_reported(db, setup):
    P.process_order(db, W.order("FO-7", subtotal=20), [W.item("FI-7", "FO-7", price=20, ext="WN-BOX")], source="api")
    stats = F.ingest_rows(db, [W.earnings("L-7", "FO-7", 22)], source="api")
    assert stats["amount_mismatch"] == 1


def test_report_still_generating_is_finished_by_the_next_sync(db, setup):
    fake = W.FakeWhatnot()
    P.process_order(db, W.order("FO-8", subtotal=20), [W.item("FI-8", "FO-8", price=20, ext="WN-BOX")], source="api")
    fake.fin_rows = [W.earnings("L-8", "FO-8", 20)]
    fake.report_polls_pending = 10_000
    with fake.client() as c:
        runs = F.request_reports(db, c, days=3)
        res = F.process_runs(db, c, runs, wait_seconds=6, poll_every=3)
    assert res["still_generating"] == 1 and ledger_entries(db, "L-8") == []
    fake.report_polls_pending = 0
    with fake.client() as c:
        F.process_runs(db, c, F.pending_runs(db))
    assert len(ledger_entries(db, "L-8")) == 2


def test_csv_upload_of_the_report_and_then_api_never_double_books(api, db, setup):
    tok, _ = setup
    P.process_order(db, W.order("FO-9", subtotal=40), [W.item("FI-9", "FO-9", price=40, ext="WN-BOX")], source="api")
    db.commit()
    rows = [W.earnings("L-9", "FO-9", 40), W.earnings("L-10", "FO-NOT-HERE", 15)]
    r = api.post("/api/v1/imports", headers=auth_header(tok), data={"import_type": "whatnot_financial"},
                 files={"file": ("seller_financial_report.csv", W.fin_csv(rows).encode(), "text/csv")})
    assert r.status_code == 201, r.text
    job = r.json()
    preview = str(job["sheets"][0]["preview"])
    assert "secret_buyer" not in preview
    staged = api.get(f"/api/v1/imports/{job['id']}/rows", headers=auth_header(tok)).json()
    assert {x["action"] for x in staged} == {"import", "import_order_missing"}
    assert "secret_buyer" not in str(staged)
    c = api.post(f"/api/v1/imports/{job['id']}/commit", headers=auth_header(tok), json={})
    assert c.status_code == 200, c.text
    assert c.json()["summary"]["booked"] == 1 and c.json()["summary"]["order_not_in_erp"] == 1
    fake = W.FakeWhatnot()
    fake.fin_rows = rows
    run = fin_sync(db, fake)
    assert run.stats["financial"]["new"] == 0 and run.stats["financial"]["existing"] >= 2
    assert len(ledger_entries(db, "L-9")) == 2


def test_show_analytics_stored_when_final(db, setup):
    fake = W.FakeWhatnot()
    fake.shows[SHOW] = W.show(SHOW)
    fake.analytics[SHOW] = W.analytics(SHOW, status="PENDING")
    with fake.client() as c:
        sync.run(db, c, days=3, kinds=("shows",))
    m = db.execute(text("SELECT count(*) FROM livestream_metrics")).scalar_one()
    assert m == 0
    fake.analytics[SHOW] = W.analytics(SHOW)
    with fake.client() as c:
        sync.run(db, c, days=3, kinds=("shows",))
    vals = dict(db.execute(text("SELECT metric_code, value FROM livestream_metrics")).all())
    assert vals["sales"] == Decimal("80") and vals["stream_minutes"] == 120 and vals["max_concurrent_viewers"] == 45


def test_show_pnl(api, db, setup):
    tok, sku = setup  # 5 boxes at $40 in the US
    fake = W.FakeWhatnot()
    fake.shows[SHOW] = W.show(SHOW)
    fake.analytics[SHOW] = W.analytics(SHOW, sales=W.money(140), estimated_earnings=W.money(124.00), orders_count=3)
    P.process_order(db, W.order("PN-1", "completed", subtotal=60, show_id=SHOW),
                    [W.item("PN-I1", "PN-1", price=60, ext="WN-BOX")], source="api")
    P.process_order(db, W.order("PN-2", "completed", subtotal=80, show_id=SHOW, discount=10),
                    [W.item("PN-I2", "PN-2", price=80, ext="WN-BOX")], source="api")
    P.process_order(db, W.order("PN-3", subtotal=20, show_id=SHOW),  # paid, not shipped yet, no fees yet
                    [W.item("PN-I3", "PN-3", price=20, ext="WN-BOX")], source="api")
    fake.fin_rows = [W.earnings("PL-1", "PN-1", 60, commission=4.80, proc=2.04, show_id=SHOW),
                     W.earnings("PL-2", "PN-2", 80, coupon=-10, commission=5.60, proc=2.33, ship=1.00, show_id=SHOW),
                     W.ledger_row("PL-T", "TIP", "3.00", show_id=SHOW)]
    with fake.client() as c:
        sync.run(db, c, days=3, kinds=("shows", "financial"))
    db.commit()
    ls_id = db.execute(text("SELECT id FROM livestreams WHERE external_id = :s"), {"s": SHOW}).scalar_one()

    _, viewer = token_for(api, db, "read_only")
    assert api.get("/api/v1/reports/livestreams", headers=auth_header(viewer)).status_code == 403
    _, ops = token_for(api, db, "us_live_ops")
    lst = api.get("/api/v1/reports/livestreams", headers=auth_header(ops), params={"from": "2026-11-01", "to": "2026-11-30"})
    assert lst.status_code == 200, lst.text
    row = next(r for r in lst.json() if r["id"] == ls_id)
    d = api.get(f"/api/v1/reports/livestreams/{ls_id}", headers=auth_header(ops)).json()
    s = d["summary"]
    assert s["sales"] == 160 and s["discounts"] == -10 and s["net_sales"] == 150
    assert s["commission"] == -10.40 and s["processing"] == -4.37 and s["shipping"] == -1.00
    assert s["cogs"] == -80 and s["other"] == 3  # 2 boxes shipped at $40 ; tip
    assert s["gross_profit"] == round(150 - 10.40 - 4.37 - 1.00 - 80 + 3, 2) == row["gross_profit"]
    cov = d["coverage"]
    assert cov["paid_orders"] == 3 and cov["orders_without_fees"] == 1 and cov["units_without_cogs"] == 1
    assert not cov["complete"] and [o["external_order_id"] for o in d["orders_without_fees"]] == ["PN-3"]
    assert d["stream_minutes"] == 120 and d["per_hour"]["gross_profit"] == round(s["gross_profit"] / 2, 2)
    cmp = d["comparison"]
    assert cmp["whatnot_sales"] == 140 and cmp["ours_sales"] == 150 and cmp["whatnot_orders"] == 3
    assert d["skus"][0]["units"] == 3 and d["skus"][0]["cogs"] == -80
    # the per-SKU rows add up to the show (the tip has no order -> unallocated)
    assert round(sum(x["gross_profit"] for x in d["skus"]) + d["unallocated"]["other"], 2) == s["gross_profit"]
