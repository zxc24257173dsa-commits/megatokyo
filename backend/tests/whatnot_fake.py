"""A stand-in for the Whatnot Seller API used ONLY in tests. Payloads follow the official
OpenAPI schemas (Order, OrderItem, Shipment, ShipmentItem, Show) of
https://developers.whatnot.com/whatnot-seller-api/openapi.json (v2.0.0)."""
import json
from urllib.parse import parse_qs, urlparse

import httpx

from app.integrations.whatnot.client import WhatnotClient


def money(amount, currency="USD"):
    return {"amount": f"{amount:.2f}" if isinstance(amount, (int, float)) else str(amount), "currency": currency}


def order(oid, status="pending_fulfillment", *, subtotal=50, show_id=None, created="2026-11-08T20:00:00Z",
          updated=None, discount=None, currency="USD", cancelled_at=None):
    return {"id": oid, "url": None, "status": status, "show_id": show_id, "transaction_type": "auction", "buyer": None,
            "amounts": {"subtotal": money(subtotal, currency), "shipping": money(5, currency), "tax": money(3, currency),
                        "applied_discount": money(discount, currency) if discount else None,
                        "total": money(subtotal + 8 - (discount or 0), currency)},
            "created_at": created, "updated_at": updated or created, "cancelled_at": cancelled_at}


def item(iid, oid, *, qty=1, price=50, sku=None, ext=None, product_id="P1", variant_id=None, shipment_id=None,
         title="Card box"):
    return {"id": iid, "order_id": oid, "product_id": product_id, "variant_id": variant_id,
            "product_external_id": ext, "variant_external_id": None,
            "purchased_item": {"title": title, "description": None, "sku": sku}, "quantity": qty,
            "unit_price": money(price), "price_includes_tax": False, "shipment_id": shipment_id}


def shipment(sid, status="dropped_off", updated="2026-11-09T15:00:00Z"):
    return {"id": sid, "url": "https://example.invalid", "status": status, "fulfillment_method": "whatnot_label",
            "recipient": None, "weight": None, "dimensions": None, "signature_required": False,
            "hazardous_material": None, "tracking": {"carrier": "usps", "tracking_code": "9400TEST"}, "documents": None,
            "insurance": None, "seller_paid_shipping_cost": money(0), "required_actions": [], "dropoff_due": None,
            "replacement_shipment_id": None, "created_at": "2026-11-09T01:00:00Z", "updated_at": updated}


def shipment_item(siid, sid, oid, order_item_id):
    return {"id": siid, "shipment_id": sid, "order_id": oid, "order_item_id": order_item_id, "dimension_profile": None,
            "hazardous_material": None, "country_of_origin": "JP", "created_at": None, "updated_at": None}


def show(show_id, status="ended", title="Friday night breaks"):
    return {"id": show_id, "title": title, "url": "https://example.invalid", "description": None, "status": status,
            "visibility": "public", "scheduled_start_at": "2026-11-07T23:00:00Z", "ended_at": "2026-11-08T02:00:00Z",
            "thumbnail": None, "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-08T02:00:00Z"}


FIN_COLUMNS = ["transaction_completed_at_utc", "order_placed_at_utc", "seller_id", "transaction_type",
               "transaction_message", "order_id", "listing_title", "listing_description", "product_category",
               "buy_format", "sale_type", "quantity_sold", "sku", "cost_of_goods", "livestream_id", "livestream_title",
               "buyer_name", "buyer_state", "buyer_country", "shipment_id", "transaction_currency",
               "transaction_amount", "buyer_paid", "original_item_price", "coupon_cost", "post_coupon_price",
               "seller_shipping_charge", "commission_fee", "commission_tax", "processing_fee", "processing_fee_tax",
               "ledger_transaction_id"]


def earnings(ledger, order_id, price, *, commission=None, ctax=0, proc=None, ptax=0, ship=0, show_id="",
             completed="2026-11-09 03:00:00.000000", coupon=0):
    """An ORDER_EARNINGS row shaped like the real report (amount = price - fees - seller shipping)."""
    commission = round(price * 0.08, 2) if commission is None else commission
    proc = round(price * 0.029 + 0.30, 2) if proc is None else proc
    post = price + coupon
    amount = round(post - commission - ctax - proc - ptax - ship, 2)
    return {"transaction_completed_at_utc": completed, "order_placed_at_utc": "2026-11-08 20:00:00.000000",
            "seller_id": "1234567", "transaction_type": "ORDER_EARNINGS",
            "transaction_message": f"Earnings for selling a X1 Promo Pack #{ledger}", "order_id": order_id,
            "listing_title": "Promo Pack #1", "listing_description": "desc", "product_category": "TCG",
            "buy_format": "AUCTION", "sale_type": "", "quantity_sold": "1", "sku": "", "cost_of_goods": "27.0",
            "livestream_id": show_id, "livestream_title": "Show", "buyer_name": "secret_buyer", "buyer_state": "CA",
            "buyer_country": "US", "shipment_id": "5001", "transaction_currency": "USD",
            "transaction_amount": f"{amount}", "buyer_paid": f"{price + 8}", "original_item_price": f"{price}",
            "coupon_cost": f"{coupon}", "post_coupon_price": f"{post}", "seller_shipping_charge": f"{ship}",
            "commission_fee": f"{commission}", "commission_tax": f"{ctax}", "processing_fee": f"{proc}",
            "processing_fee_tax": f"{ptax}", "ledger_transaction_id": ledger}


def ledger_row(ledger, ttype, amount, *, order_id="", message="", completed="2026-11-09 05:00:00.000000", show_id=""):
    """Non-earnings transaction (refund, shipping charge, tip...): order_placed_at 1970 and shipment 0, as in the real file."""
    row = {c: "" for c in FIN_COLUMNS}
    row.update(transaction_completed_at_utc=completed, order_placed_at_utc="1970-01-01 00:00:00",
               seller_id="1234567", transaction_type=ttype, transaction_message=message, order_id=order_id,
               livestream_id=show_id, shipment_id="0", transaction_currency="USD", transaction_amount=f"{amount}",
               ledger_transaction_id=ledger)
    return row


def fin_csv(rows) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIN_COLUMNS)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def analytics(show_id, status="GENERATED", **over):
    a = {"show_id": show_id, "title": "Friday night breaks", "start_time": "2026-11-07T23:00:00.000Z",
         "calculation_status": status, "sales": money(80), "estimated_earnings": money(68.12), "orders_count": 2,
         "average_order_value": money(40), "giveaway_orders_count": 0, "giveaway_spend": None,
         "show_impression_count": 1500, "show_clickthrough_rate": 4.2, "buyers_count": 2,
         "first_time_buyers_count": 1, "returning_buyers_count": 1, "giveaway_winners_count": 0,
         "first_time_giveaway_winners_count": 0, "returning_giveaway_winners_count": 0, "buyer_shares_count": 3,
         "stream_minutes": 120, "max_concurrent_viewers": 45, "total_views": 300, "distinct_viewers_count": 210,
         "average_rating": 4.9}
    if status != "GENERATED":
        a.update({k: None for k in a if k not in ("show_id", "title", "start_time", "calculation_status")})
    a.update(over)
    return a


class FakeWhatnot:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.items: dict[str, list] = {}
        self.shipments: dict[str, dict] = {}
        self.shipment_items: dict[str, list] = {}
        self.shows: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_next: list[int] = []  # status codes to return for the next requests
        self.fin_rows: list[dict] = []  # rows of the seller_financial_report
        self.reports: dict[str, dict] = {}
        self.report_polls_pending = 0  # GET /reports/{id} answers in_progress this many times first
        self.analytics: dict[str, dict] = {}
        self.downloads: list[httpx.Request] = []

    def add_order(self, o, items):
        self.orders[o["id"]] = o
        self.items[o["id"]] = items

    def _page(self, rows, params):
        limit = int(params.get("limit", ["10"])[0])
        start = int(params.get("cursor", ["0"])[0] or 0)
        chunk = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else None
        return {"data": chunk, "next_cursor": nxt}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        if url.hostname == "seller-reports.example":  # presigned report download
            self.downloads.append(request)
            rid = url.path.rsplit("/", 1)[-1].removesuffix(".csv")
            return httpx.Response(200, text=self.reports[rid]["_csv"])
        path = url.path.split("/seller-api/rest/v2", 1)[-1]
        params = parse_qs(url.query)
        self.calls.append(f"{request.method} {path}")
        if self.fail_next:
            code = self.fail_next.pop(0)
            headers = {"Retry-After": "1"} if code == 429 else {}
            return httpx.Response(code, headers=headers, json={"type": "rate-limited" if code == 429 else "server-error",
                                                                "title": "x", "status": code, "detail": "try again"})
        parts = [p for p in path.split("/") if p]
        if parts == ["users", "me"]:
            return httpx.Response(200, json={"id": "1234567", "username": "megatokyo_test", "display_name": "Megatokyo",
                                             "seller_currency": "USD", "country_code": "US", "can_go_live": True,
                                             "image": None})
        if parts == ["orders"]:
            rows = sorted(self.orders.values(), key=lambda o: o["created_at"], reverse=True)
            return httpx.Response(200, json=self._page(rows, params))
        if len(parts) == 2 and parts[0] == "orders":
            o = self.orders.get(parts[1])
            return httpx.Response(200, json=o) if o else self._404("order-not-found")
        if len(parts) == 3 and parts[0] == "orders" and parts[2] == "items":
            return httpx.Response(200, json=self._page(self.items.get(parts[1], []), params))
        if parts == ["shipments"]:
            rows = sorted(self.shipments.values(), key=lambda s: s["created_at"], reverse=True)
            return httpx.Response(200, json=self._page(rows, params))
        if len(parts) == 2 and parts[0] == "shipments":
            s = self.shipments.get(parts[1])
            return httpx.Response(200, json=s) if s else self._404("shipment-not-found")
        if len(parts) == 3 and parts[0] == "shipments" and parts[2] == "items":
            return httpx.Response(200, json=self._page(self.shipment_items.get(parts[1], []), params))
        if parts == ["shows"]:
            return httpx.Response(200, json=self._page(list(self.shows.values()), params))
        if len(parts) == 2 and parts[0] == "shows":
            s = self.shows.get(parts[1])
            return httpx.Response(200, json=s) if s else self._404("show-not-found")
        if parts == ["reports"] and request.method == "POST":
            params_in = json.loads(request.content)["report_parameters"]
            rid = f"0199a7c2-0000-7000-8000-{len(self.reports) + 1:012d}"
            self.reports[rid] = {"id": rid, "report_type": params_in["report_type"], "status": "pending",
                                 "report_parameters": params_in, "report_url": None,
                                 "created_at": "2026-11-09T18:00:00.000Z", "updated_at": "2026-11-09T18:00:00.000Z",
                                 "_csv": fin_csv(self.fin_rows)}
            return httpx.Response(201, json={k: v for k, v in self.reports[rid].items() if k != "_csv"})
        if len(parts) == 2 and parts[0] == "reports":
            r = self.reports.get(parts[1])
            if r is None:
                return self._404("report-not-found")
            if self.report_polls_pending > 0:
                self.report_polls_pending -= 1
                return httpx.Response(200, json={**{k: v for k, v in r.items() if k != "_csv"}, "status": "in_progress"})
            r.update(status="completed", report_url=f"https://seller-reports.example/seller-reports/{r['id']}.csv")
            return httpx.Response(200, json={k: v for k, v in r.items() if k != "_csv"})
        if len(parts) == 3 and parts[:2] == ["analytics", "shows"]:
            a = self.analytics.get(parts[2])
            return httpx.Response(200, json=a) if a else self._404("show-not-found")
        return self._404("not-found")

    @staticmethod
    def _404(t):
        return httpx.Response(404, json={"type": t, "title": "Not found", "status": 404, "detail": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> WhatnotClient:
        return WhatnotClient("wn_pat_tk_test", base_url="https://api.whatnot.com/seller-api/rest/v2",
                             transport=httpx.MockTransport(self.handler), sleep=lambda s: None)


def body(payload: dict) -> bytes:
    return json.dumps(payload).encode()
