"""Whatnot webhooks (Standard Webhooks specification).

  verify : HMAC-SHA256 over "{webhook-id}.{webhook-timestamp}.{raw body}" with the base64 key that
           follows the `wn_wh_` prefix; header `webhook-signature` holds space-separated "v1,<b64>";
           timestamps older/newer than 5 minutes are rejected (replay protection)
  receive: store once per webhook-id (deliveries are at-least-once), answer 2xx fast
  process: re-read the current state from the API (never trust ordering), then apply it
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.integrations.whatnot import account
from app.integrations.whatnot import processing as P
from app.integrations.whatnot.client import WhatnotClient
from app.models.integration import WebhookEvent

log = logging.getLogger("app.whatnot.webhooks")
TOLERANCE_SECONDS = 300
KEPT_HEADERS = ("webhook-id", "webhook-timestamp", "content-type", "user-agent")


def _signing_key(secret: str) -> bytes:
    for prefix in ("wn_wh_", "whsec_"):
        if secret.startswith(prefix):
            secret = secret[len(prefix):]
    return base64.b64decode(secret)


def verify(secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    wid, ts, sig_header = headers.get("webhook-id"), headers.get("webhook-timestamp"), headers.get("webhook-signature")
    if not (wid and ts and sig_header):
        return False
    try:
        if abs((now if now is not None else time.time()) - int(ts)) > TOLERANCE_SECONDS:
            return False
        key = _signing_key(secret)
    except (ValueError, binascii.Error):
        return False
    expected = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    for part in sig_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


def receive(db: Session, headers: dict, body: bytes) -> tuple[str, int | None]:
    """Returns (outcome, event_id). outcome: stored | duplicate | invalid_signature | not_configured | bad_payload."""
    secret = account.webhook_secret(db)
    if not secret:
        return "not_configured", None
    if not verify(secret, headers, body):
        return "invalid_signature", None
    try:
        payload = json.loads(body)
    except ValueError:
        return "bad_payload", None
    row = db.execute(text("""
        INSERT INTO webhook_events (platform, topic, seller_id, dedupe_key, headers, payload, signature_valid, status)
        VALUES ('whatnot', :topic, :seller, :key, CAST(:headers AS jsonb), CAST(:payload AS jsonb), true, 'pending')
        ON CONFLICT (platform, dedupe_key) DO NOTHING RETURNING id"""),
        {"topic": str(payload.get("type", "unknown"))[:80], "seller": str(payload.get("seller_id") or "")[:128] or None,
         "key": headers["webhook-id"][:200],
         "headers": json.dumps({k: headers.get(k) for k in KEPT_HEADERS}),
         "payload": body.decode()}).first()
    db.commit()
    return ("stored", row[0]) if row else ("duplicate", None)


def process_event(db: Session, client: WhatnotClient, event_id: int) -> str:
    ev = db.scalar(select(WebhookEvent).where(WebhookEvent.id == event_id).with_for_update())
    if ev is None or ev.status == "processed":
        return "skipped"
    acc = account.get_account(db)
    if acc and acc.seller_id and ev.seller_id and ev.seller_id != acc.seller_id:
        ev.status, ev.last_error = "ignored", "event for a different seller"
        db.commit()
        return ev.status
    ev.attempts += 1
    topic, data = ev.topic, ev.payload.get("data") or {}
    try:
        with db.begin_nested():
            if topic.startswith("orders."):
                order = client.order(data["id"])  # current state, not the possibly stale payload
                P.process_order(db, order, client.order_items(order["id"]), source="webhook")
            elif topic == "shipments.replaced":
                for key in ("previous_shipment", "replacement_shipment"):
                    sid = (data.get(key) or {}).get("id")
                    if sid:
                        P.process_shipment(db, client.shipment(sid), client.shipment_items(sid), source="webhook")
            elif topic.startswith("shipments."):
                sid = data.get("id") or data.get("shipment_id")
                P.process_shipment(db, client.shipment(sid), client.shipment_items(sid), source="webhook")
            elif topic.startswith("shows."):
                P.upsert_show(db, client.show(data.get("id") or data.get("show_id")), "webhook")
            else:
                ev.status = "ignored"  # products / merchandising: not needed by the ERP yet
        if ev.status != "ignored":
            ev.status = "processed"
        ev.processed_at, ev.last_error = datetime.now(UTC), None
    except Exception as exc:
        db.rollback()
        ev = db.get(WebhookEvent, event_id)
        ev.attempts += 1
        ev.status, ev.last_error = "failed", str(exc)[:1000]
        log.exception("webhook processing failed")
    db.commit()
    return ev.status


def process_pending(db: Session, client: WhatnotClient, limit: int = 200) -> dict:
    """Retry failed/pending events (called by the sync job)."""
    ids = list(db.scalars(select(WebhookEvent.id).where(WebhookEvent.platform == "whatnot",
                                                        WebhookEvent.status.in_(("pending", "failed")),
                                                        WebhookEvent.attempts < 10)
                          .order_by(WebhookEvent.id).limit(limit)))
    out: dict = {}
    for i in ids:
        r = process_event(db, client, i)
        out[r] = out.get(r, 0) + 1
    return out
