import base64
import hashlib
import hmac
import time
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from app.core.config import get_settings
from app.integrations.whatnot import processing as P
from app.integrations.whatnot import sync, webhooks
from app.integrations.whatnot.client import WhatnotError
from app.models.sales import SalesOrder, SalesOrderLine
from tests import whatnot_fake as W
from tests.conftest import auth_header, idem, token_for
from tests.test_catalog import EDITOR, create_product


# ---------- helpers ----------

def us_stock(api, tok, sku_id, qty, cost="10"):
    r = api.post("/api/v1/inventory/opening-stock", headers={**auth_header(tok), **idem()},
                 json={"location_code": "US_MAIN", "lines": [{"sku_id": sku_id, "qty": qty, "unit_cost_usd": cost}]})
    assert r.status_code == 201, r.text


def pos(db, sku_id):
    return db.execute(text("SELECT * FROM v_inventory_position WHERE sku_id = :s"), {"s": sku_id}).mappings().one()


def entries(db, order_ext):
    return db.execute(text("""SELECT e.entry_type, e.amount_base FROM financial_entries e
                               JOIN sales_orders o ON o.id = e.sales_order_id
                              WHERE o.external_order_id = :o ORDER BY e.id"""), {"o": order_ext}).all()


def line_of(db, item_id) -> SalesOrderLine:
    return db.scalar(select(SalesOrderLine).where(SalesOrderLine.external_line_id == item_id))


@pytest.fixture
def setup(api, db):
    _, tok = token_for(api, db, *EDITOR)
    sku = create_product(api, tok, codes=("WN-BOX",))["sku_ids"][0]
    us_stock(api, tok, sku, 5, cost="40")
    return tok, sku


# ---------- client ----------

def test_client_paginates_retries_429_and_reports_problem_types():
    fake = W.FakeWhatnot()
    for n in range(7):
        fake.add_order(W.order(f"O{n}", created=f"2026-11-0{n + 1}T00:00:00Z"), [])
    fake.fail_next = [429, 503]
    with fake.client() as c:
        ids = [o["id"] for o in c.paginate("/orders", limit=3)]
        assert ids == [f"O{n}" for n in range(6, -1, -1)]  # newest first, across 3 pages, after 2 retries
        with pytest.raises(WhatnotError) as e:
            c.order("missing")
    assert e.value.problem_type == "order-not-found" and e.value.status == 404


# ---------- order processing ----------

def test_paid_order_books_revenue_and_reserves_once(db, setup):
    _, sku = setup
    o = W.order("ORD-1", subtotal=120)
    items = [W.item("IT-1", "ORD-1", qty=2, price=60, ext="wn-box", product_id="WP-9")]
    for _ in range(3):  # webhook + retry + sync: same result
        P.process_order(db, o, items, source="webhook")
    assert entries(db, "ORD-1") == [("revenue", Decimal("120.0000"))]
    p = pos(db, sku)
    assert (p["us_on_hand"], p["us_reserved"], p["us_available"]) == (5, 2, 3)
    assert line_of(db, "IT-1").stock_state == "reserved"
    mapped = db.execute(text("SELECT sku_id FROM external_sku_mappings WHERE external_product_id = 'WP-9'")).scalar_one()
    assert mapped == sku  # learned from the external ID, used for future orders


def test_pending_order_books_nothing_until_paid(db, setup):
    _, sku = setup
    items = [W.item("IT-P", "ORD-P", ext="WN-BOX")]
    P.process_order(db, W.order("ORD-P", status="pending"), items, source="api")
    assert entries(db, "ORD-P") == [] and pos(db, sku)["us_reserved"] == 0
    P.process_order(db, W.order("ORD-P", updated="2026-11-08T20:05:00Z"), items, source="api")
    assert entries(db, "ORD-P") == [("revenue", Decimal("50.0000"))] and pos(db, sku)["us_reserved"] == 1


def test_stale_update_is_ignored(db, setup):
    _, sku = setup
    items = [W.item("IT-S", "ORD-S", ext="WN-BOX")]
    P.process_order(db, W.order("ORD-S", status="cancelled", updated="2026-11-08T22:00:00Z"), items, source="webhook")
    P.process_order(db, W.order("ORD-S", status="pending_fulfillment", updated="2026-11-08T21:00:00Z"), items,
                    source="webhook")  # older event delivered late
    assert db.scalar(select(SalesOrder.status).where(SalesOrder.external_order_id == "ORD-S")) == "cancelled"
    assert pos(db, sku)["us_reserved"] == 0


def test_shipment_moves_stock_fifo_and_books_cogs(api, db, setup):
    tok, sku = setup
    us_stock(api, tok, sku, 5, cost="50")  # second lot, more expensive
    items = [W.item("IT-2", "ORD-2", qty=7, price=60, ext="WN-BOX", shipment_id="SH-2")]
    P.process_order(db, W.order("ORD-2", subtotal=420), items, source="api")
    sh = W.shipment("SH-2", status="label_generated")
    P.process_shipment(db, sh, [W.shipment_item("SI-1", "SH-2", "ORD-2", "IT-2")], source="api")
    assert line_of(db, "IT-2").stock_state == "reserved"  # a label is not a shipment
    for _ in range(2):
        P.process_shipment(db, W.shipment("SH-2", status="dropped_off", updated="2026-11-09T16:00:00Z"),
                           [W.shipment_item("SI-1", "SH-2", "ORD-2", "IT-2")], source="api")
    line = line_of(db, "IT-2")
    assert line.stock_state == "shipped"
    p = pos(db, sku)
    assert (p["us_on_hand"], p["us_reserved"], p["us_available"]) == (3, 0, 3)
    # FIFO: 5 x $40 + 2 x $50 = $300 ; profit so far = 420 - 300
    assert entries(db, "ORD-2") == [("revenue", Decimal("420.0000")), ("cogs", Decimal("-200.0000")),
                                    ("cogs", Decimal("-100.0000"))]
    assert db.execute(text("SELECT sum(total_cost_base) FROM cogs_allocations WHERE order_line_id = :l"),
                      {"l": line.id}).scalar_one() == Decimal("300.0000")


def test_completed_order_counts_as_shipped(db, setup):
    _, sku = setup
    P.process_order(db, W.order("ORD-C", status="completed"), [W.item("IT-C", "ORD-C", ext="WN-BOX")], source="api")
    assert line_of(db, "IT-C").stock_state == "shipped" and pos(db, sku)["us_on_hand"] == 4


def test_bundle_order_consumes_components(api, db, setup):
    tok, box = setup
    pack = create_product(api, tok, codes=("WN-PACK",))["sku_ids"][0]
    us_stock(api, tok, pack, 20, cost="3")
    r = api.post("/api/v1/bundles", headers=auth_header(tok), json={
        "sku_code": "WN-SET", "name": "set", "components": [{"sku_id": box, "qty": 1}, {"sku_id": pack, "qty": 5}]})
    assert r.status_code == 201
    P.process_order(db, W.order("ORD-B", status="completed", subtotal=100),
                    [W.item("IT-B", "ORD-B", qty=2, price=50, ext="WN-SET")], source="api")
    assert (pos(db, box)["us_on_hand"], pos(db, pack)["us_on_hand"]) == (3, 10)
    cogs = sum(a for t, a in entries(db, "ORD-B") if t == "cogs")
    assert cogs == Decimal("-110.0000")  # 2 x $40 + 10 x $3


def test_cancel_before_shipping_releases_and_reverses(db, setup):
    _, sku = setup
    items = [W.item("IT-X", "ORD-X", ext="WN-BOX")]
    P.process_order(db, W.order("ORD-X", discount=5), items, source="api")
    P.process_order(db, W.order("ORD-X", status="cancelled", updated="2026-11-08T21:00:00Z", discount=5,
                                cancelled_at="2026-11-08T21:00:00Z"), items, source="webhook")
    assert line_of(db, "IT-X").stock_state == "released" and pos(db, sku)["us_available"] == 5
    assert sum(a for _, a in entries(db, "ORD-X")) == 0  # revenue, coupon and both reversals net to zero
    assert {t for t, _ in entries(db, "ORD-X")} == {"revenue", "sales_discount"}


def test_cancel_after_shipping_needs_a_person(db, setup):
    items = [W.item("IT-Y", "ORD-Y", ext="WN-BOX")]
    P.process_order(db, W.order("ORD-Y", status="completed"), items, source="api")
    P.process_order(db, W.order("ORD-Y", status="cancelled", updated="2026-11-09T21:00:00Z"), items, source="api")
    line = line_of(db, "IT-Y")
    assert line.stock_state == "shipped" and line.exception_code == "cancelled_after_shipping"


def test_unmapped_sku_then_manual_mapping(api, db, setup):
    tok, sku = setup
    P.process_order(db, W.order("ORD-U"), [W.item("IT-U", "ORD-U", title="Mystery box", product_id="WP-77")],
                    source="api")
    line = line_of(db, "IT-U")
    assert line.exception_code == "unmapped_sku" and entries(db, "ORD-U") == [("revenue", Decimal("50.0000"))]
    P.process_order(db, W.order("ORD-U2"), [W.item("IT-U2", "ORD-U2", product_id="WP-77")], source="api")
    db.commit()
    r = api.post(f"/api/v1/orders/lines/{line.id}/map", headers=auth_header(tok), json={"sku_id": sku})
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["stock_state"] == "reserved" and r.json()["lines"][0]["exception_code"] is None
    assert line_of(db, "IT-U2").stock_state == "reserved"  # same Whatnot product: fixed too
    P.process_order(db, W.order("ORD-U3"), [W.item("IT-U3", "ORD-U3", product_id="WP-77")], source="api")
    assert line_of(db, "IT-U3").sku_id == sku  # and every future order
    assert pos(db, sku)["us_reserved"] == 3


def test_oversold_and_insufficient_stock_are_exceptions_then_recover(api, db, setup):
    tok, sku = setup
    P.process_order(db, W.order("ORD-O", status="pending_fulfillment"),
                    [W.item("IT-O", "ORD-O", qty=8, ext="WN-BOX", shipment_id="SH-O")], source="api")
    assert line_of(db, "IT-O").exception_code == "oversold"
    P.process_shipment(db, W.shipment("SH-O"), [W.shipment_item("SI-O", "SH-O", "ORD-O", "IT-O")], source="api")
    line = line_of(db, "IT-O")
    assert line.stock_state == "reserved" and line.exception_code == "insufficient_stock"
    assert pos(db, sku)["us_on_hand"] == 5  # nothing moved: never negative
    us_stock(api, tok, sku, 3, cost="45")  # the missing stock is booked in
    stats = {}
    sync.retry_exceptions(db, stats)
    line = line_of(db, "IT-O")
    assert line.stock_state == "shipped" and line.exception_code is None and pos(db, sku)["us_on_hand"] == 0


def test_non_usd_order_is_flagged(db, setup):
    P.process_order(db, W.order("ORD-J", currency="JPY"), [W.item("IT-J", "ORD-J", ext="WN-BOX")], source="api")
    assert line_of(db, "IT-J").exception_code == "non_usd" and entries(db, "ORD-J") == []


# ---------- webhooks ----------

def test_signature_matches_whatnot_documentation_example():
    """Test vector published at developers.whatnot.com/getting-started/webhooks."""
    secret = "wn_wh_AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="
    body = (b'{"type": "shows.started", "timestamp": "2026-10-20T16:00:00.123Z", "data": {"show_id": '
            b'"9b2f6c1e-4d3a-4f8b-a1c2-3d4e5f6a7b8c"}, "version": "v2", "seller_id": "1234567"}')
    headers = {"webhook-id": "0b7e4f6c-3d1a-4a8e-9f2b-5c6d7e8f9a0b", "webhook-timestamp": "1792512005",
               "webhook-signature": "v1,8m/VX9GLLqhNZJndMpPw7OTUKSDI4szDXAmIFY8ffBI="}
    assert webhooks.verify(secret, headers, body, now=1792512005)
    assert not webhooks.verify(secret, headers, body + b" ", now=1792512005)          # body changed
    assert not webhooks.verify(secret, headers, body, now=1792512005 + 301)           # replayed later
    assert not webhooks.verify(secret, {**headers, "webhook-id": "x"}, body, now=1792512005)


SECRET = "wn_wh_" + base64.b64encode(b"k" * 32).decode()


def signed(payload: dict, wid="evt-1", ts=None):
    ts = str(ts or int(time.time()))
    b = W.body(payload)
    sig = base64.b64encode(hmac.new(b"k" * 32, f"{wid}.{ts}.".encode() + b, hashlib.sha256).digest()).decode()
    return b, {"webhook-id": wid, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
               "content-type": "application/json"}


def test_webhook_endpoint_verifies_dedupes_and_processes(api, db, setup, monkeypatch):
    _, sku = setup
    monkeypatch.setattr(get_settings(), "whatnot_webhook_secret", SecretStr(SECRET))
    monkeypatch.setattr("app.api.v1.whatnot._process_event_job", lambda event_id: None)  # processed below
    fake = W.FakeWhatnot()
    fake.add_order(W.order("ORD-W"), [W.item("IT-W", "ORD-W", ext="WN-BOX")])
    payload = {"type": "orders.created", "timestamp": "2026-11-08T20:00:00.000Z", "data": W.order("ORD-W", status="pending"),
               "version": "v2", "seller_id": "1234567"}
    b, h = signed(payload)
    bad = dict(h, **{"webhook-signature": "v1,AAAA"})
    assert api.post("/api/v1/webhooks/whatnot", content=b, headers=bad).status_code == 401
    r1 = api.post("/api/v1/webhooks/whatnot", content=b, headers=h)
    r2 = api.post("/api/v1/webhooks/whatnot", content=b, headers=h)  # Whatnot retry
    assert r1.status_code == r2.status_code == 200 and r2.json()["duplicate"] is True
    ev_id = db.execute(text("SELECT id FROM webhook_events WHERE dedupe_key = 'evt-1'")).scalar_one()
    with fake.client() as c:
        assert webhooks.process_event(db, c, ev_id) == "processed"
    # the payload said "pending" but the API now says paid: current state wins
    assert db.scalar(select(SalesOrder.status).where(SalesOrder.external_order_id == "ORD-W")) == "pending_fulfillment"
    assert pos(db, sku)["us_reserved"] == 1
    old_b, old_h = signed(payload, wid="evt-old", ts=int(time.time()) - 3600)
    assert api.post("/api/v1/webhooks/whatnot", content=old_b, headers=old_h).status_code == 401


def test_event_for_another_seller_is_ignored(db, setup):
    from app.integrations.whatnot import account
    account.mark_connected(db, me={"id": "1234567", "username": "us"})
    db.execute(text("""INSERT INTO webhook_events (platform, topic, seller_id, dedupe_key, payload, signature_valid)
                       VALUES ('whatnot', 'orders.created', '999', 'evt-x', '{"data": {"id": "Z"}}', true)"""))
    ev = db.execute(text("SELECT id FROM webhook_events WHERE dedupe_key = 'evt-x'")).scalar_one()
    with W.FakeWhatnot().client() as c:
        assert webhooks.process_event(db, c, ev) == "ignored"


# ---------- sync ----------

def test_sync_reads_recent_orders_shipments_and_shows(db, setup):
    _, sku = setup
    fake = W.FakeWhatnot()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    old = "2025-01-01T00:00:00Z"
    fake.shows["SHOW-1"] = W.show("SHOW-1")
    for n in range(3):
        fake.add_order(W.order(f"S-{n}", created=now, show_id="SHOW-1"),
                       [W.item(f"SI-{n}", f"S-{n}", ext="WN-BOX", shipment_id="SHIP-1")])
    fake.add_order(W.order("S-OLD", created=old), [W.item("SI-OLD", "S-OLD", ext="WN-BOX")])
    ship = W.shipment("SHIP-1")
    ship["created_at"] = now
    fake.shipments["SHIP-1"] = ship
    fake.shipment_items["SHIP-1"] = [W.shipment_item(f"X{n}", "SHIP-1", f"S-{n}", f"SI-{n}") for n in range(3)]
    with fake.client() as c:
        run = sync.run(db, c, days=3)
    assert run.status == "succeeded", run.stats
    assert run.stats["orders"] == 3 and run.stats["shipments"] == 1 and run.stats["shows"] == 1
    assert db.scalar(select(SalesOrder.id).where(SalesOrder.external_order_id == "S-OLD")) is None  # outside window
    assert pos(db, sku)["us_on_hand"] == 2  # 3 boxes shipped
    title = db.execute(text("""SELECT l.title FROM sales_orders o JOIN livestreams l ON l.id = o.livestream_id
                               WHERE o.external_order_id = 'S-0'""")).scalar_one()
    assert title == "Friday night breaks"


# ---------- integration settings ----------

def test_credentials_are_validated_encrypted_and_never_returned(api, db):
    from tests.test_users_rbac import admin_token
    _, tok = admin_token(api, db)
    bad = api.put("/api/v1/integrations/whatnot/credentials", headers=auth_header(tok), json={"token": "not-a-token-x"})
    assert bad.status_code == 422
    r = api.put("/api/v1/integrations/whatnot/credentials", headers=auth_header(tok),
                json={"token": "wn_pat_tk_secret_value_123", "webhook_secret": SECRET})
    assert r.status_code == 204
    raw = db.execute(text("SELECT access_token_enc FROM integration_accounts")).scalar_one()
    assert b"wn_pat_tk_secret_value_123" not in bytes(raw)
    s = api.get("/api/v1/integrations/whatnot", headers=auth_header(tok)).json()
    assert s["token_source"] == "saved" and s["webhook_secret_configured"] is True
    assert "wn_pat_tk" not in str(s) and s["webhook_url"].endswith("/api/v1/webhooks/whatnot")
    _, ops = token_for(api, db, "us_live_ops")
    assert api.get("/api/v1/integrations/whatnot", headers=auth_header(ops)).status_code == 403
