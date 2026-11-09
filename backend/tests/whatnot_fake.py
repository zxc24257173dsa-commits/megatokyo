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


class FakeWhatnot:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.items: dict[str, list] = {}
        self.shipments: dict[str, dict] = {}
        self.shipment_items: dict[str, list] = {}
        self.shows: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_next: list[int] = []  # status codes to return for the next requests

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
        return self._404("not-found")

    @staticmethod
    def _404(t):
        return httpx.Response(404, json={"type": t, "title": "Not found", "status": 404, "detail": "not found"})

    def client(self) -> WhatnotClient:
        return WhatnotClient("wn_pat_tk_test", base_url="https://api.whatnot.com/seller-api/rest/v2",
                             transport=httpx.MockTransport(self.handler), sleep=lambda s: None)


def body(payload: dict) -> bytes:
    return json.dumps(payload).encode()
