"""Inbound parcels Japan -> US, lead time, and the US outbound backlog (Phase 9).

A parcel (shipments row, SUPPLIER -> US_MAIN) is one physical shipment with one tracking number,
carrying quantities of one or more purchase lines:

  create   each line: SUPPLIER -> IN_TRANSIT through purchasing.ship_line, so the FIFO lot is created at
           the line's landed cost and remembers its parcel line. Not sellable yet.
  receive  per line: received qty  IN_TRANSIT -> US_MAIN (sellable now)
                     lost qty      IN_TRANSIT -> LOSS (reason required; booked as inventory loss in the P&L)
                     the rest stays in transit (more boxes coming) - receive again later
  status   shipped -> received (everything received or written off) ; events are append-only

Overdue = not fully received and past its ETA. Actual lead time = shipped -> fully received, in days.
"""
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.inventory import InventoryMovement
from app.models.logistics import Carrier, Shipment, ShipmentEvent, ShipmentLine
from app.models.purchasing import PurchaseLine
from app.services import audit, ledger, purchasing
from app.services.audit import RequestContext
from app.services.inventory import location, post_movement


def _event(db: Session, shipment_id: int, event: str, user_id: int | None, data: dict) -> None:
    db.add(ShipmentEvent(shipment_id=shipment_id, event=event, user_id=user_id, data=data))


def _lock(db: Session, shipment_id: int) -> Shipment:
    sh = db.get(Shipment, shipment_id, with_for_update=True, populate_existing=True)
    if sh is None:
        raise AppError("not_found", "Parcel not found", 404)
    return sh


def create_parcel(db: Session, *, actor_id: int, lines: list[dict], shipped_on: date | None, carrier_id: int | None,
                  tracking_no: str | None, eta: date | None, packages: int | None, note: str | None,
                  idempotency_key: str, ctx: RequestContext) -> Shipment:
    if done := db.scalar(select(Shipment).where(Shipment.idempotency_key == idempotency_key)):
        return done
    lines = [x for x in lines if x.get("qty")]
    if not lines:
        raise AppError("empty_parcel", "Add at least one item to the parcel", 422)
    if len({x["purchase_line_id"] for x in lines}) != len(lines):
        raise AppError("duplicate_line", "Each purchase line can appear once per parcel", 422)
    if carrier_id is not None and db.get(Carrier, carrier_id) is None:
        raise AppError("invalid_carrier", "Unknown carrier", 422)
    when = purchasing._when(shipped_on)
    # check every line before writing anything: never ship more than was purchased
    for x in lines:
        pl = db.scalar(select(PurchaseLine).where(PurchaseLine.id == x["purchase_line_id"]).with_for_update())
        if pl is None:
            raise AppError("not_found", f"Purchase line {x['purchase_line_id']} not found", 404)
        shipped = purchasing.line_status(db, pl.id)["shipped"]
        if shipped + x["qty"] > pl.qty:
            raise AppError("over_shipment", "Cannot ship more than was purchased", 409,
                           {"purchase_line_id": pl.id, "purchased": pl.qty, "already_shipped": shipped,
                            "requested": x["qty"]})
    lead = purchasing.lead_time_days(db)
    eta = eta or (ledger.biz_date(when) + timedelta(days=lead))
    n = (db.scalar(text("SELECT count(*) FROM shipments WHERE shipment_no LIKE :p"),
                   {"p": f"P{when:%y%m%d}-%"}) or 0) + 1
    sh = Shipment(shipment_no=f"P{when:%y%m%d}-{n:02d}", from_location_id=location(db, "SUPPLIER").id,
                  to_location_id=location(db, "US_MAIN").id, carrier_id=carrier_id,
                  tracking_no=(tracking_no or "").strip()[:128] or None, status="shipped", shipped_at=when, eta=eta,
                  packages=packages, note=(note or "").strip() or None, created_by=actor_id,
                  idempotency_key=idempotency_key)
    db.add(sh)
    db.flush()
    for x in lines:
        pl = db.get(PurchaseLine, x["purchase_line_id"])
        if pl is None:
            raise AppError("not_found", f"Purchase line {x['purchase_line_id']} not found", 404)
        sl = ShipmentLine(shipment_id=sh.id, sku_id=pl.sku_id, purchase_line_id=pl.id, qty_planned=x["qty"],
                          qty_shipped=x["qty"])
        db.add(sl)
        db.flush()
        # same checks as a single-line shipment: never more than purchased; FIFO lot at landed cost
        purchasing.ship_line(db, actor_id=actor_id, line_id=pl.id, qty=x["qty"], shipped_on=shipped_on,
                             idempotency_key=f"parcel:{idempotency_key}:{pl.id}", ctx=ctx, shipment_line_id=sl.id)
    _event(db, sh.id, "shipped", actor_id, {"lines": len(lines), "units": sum(x["qty"] for x in lines),
                                            "tracking_no": sh.tracking_no, "eta": eta.isoformat()})
    audit.record(db, action="parcel.shipped", entity_type="shipment", entity_id=sh.id, actor_id=actor_id,
                 after={"shipment_no": sh.shipment_no, "lines": lines, "tracking_no": sh.tracking_no,
                        "eta": eta.isoformat()}, ctx=ctx)
    db.flush()
    return sh


def update_parcel(db: Session, *, actor_id: int, shipment_id: int, tracking_no: str | None, carrier_id: int | None,
                  eta: date | None, packages: int | None, note: str | None, ctx: RequestContext) -> Shipment:
    sh = _lock(db, shipment_id)
    before = {"tracking_no": sh.tracking_no, "carrier_id": sh.carrier_id, "eta": str(sh.eta), "note": sh.note}
    if carrier_id is not None and db.get(Carrier, carrier_id) is None:
        raise AppError("invalid_carrier", "Unknown carrier", 422)
    sh.tracking_no = (tracking_no or "").strip()[:128] or None
    sh.carrier_id, sh.eta, sh.packages, sh.note = carrier_id, eta or sh.eta, packages, (note or "").strip() or None
    db.flush()
    _event(db, sh.id, "updated", actor_id, {"tracking_no": sh.tracking_no, "eta": str(sh.eta)})
    audit.record(db, action="parcel.updated", entity_type="shipment", entity_id=sh.id, actor_id=actor_id,
                 before=before, after={"tracking_no": sh.tracking_no, "carrier_id": sh.carrier_id,
                                       "eta": str(sh.eta), "note": sh.note}, ctx=ctx)
    return sh


def _in_transit_lots(db: Session, sl: ShipmentLine):
    return db.execute(text("""
        SELECT b.lot_id, b.qty FROM inventory_balances b JOIN inventory_lots l ON l.id = b.lot_id
         WHERE l.shipment_line_id = :sl AND b.location_id = :transit AND b.qty > 0
         ORDER BY l.id FOR UPDATE OF b"""), {"sl": sl.id, "transit": location(db, "IN_TRANSIT").id}).all()


def receive_parcel(db: Session, *, actor_id: int, shipment_id: int, lines: list[dict], received_on: date | None,
                   idempotency_key: str, ctx: RequestContext) -> Shipment:
    """lines: [{shipment_line_id, received, lost, lost_reason}] ; anything not received or lost stays in transit."""
    sh = _lock(db, shipment_id)
    key = f"parcelrecv:{idempotency_key}"
    if db.scalar(select(InventoryMovement.id).where(InventoryMovement.idempotency_key.like(f"{key}:%"))):
        return sh  # same submit again
    if sh.status not in ("shipped", "delivered"):
        raise AppError("parcel_closed", "This parcel is already fully received", 409)
    when = purchasing._when(received_on)
    if when < sh.shipped_at:
        raise AppError("invalid_date", "Received date is before the ship date", 422)
    touched = 0
    for x in lines:
        received, lost = int(x.get("received") or 0), int(x.get("lost") or 0)
        if received < 0 or lost < 0:
            raise AppError("invalid_qty", "Quantities cannot be negative", 422)
        if not received and not lost:
            continue
        sl = db.get(ShipmentLine, x["shipment_line_id"], with_for_update=True, populate_existing=True)
        if sl is None or sl.shipment_id != sh.id:
            raise AppError("not_found", "Line not in this parcel", 404)
        reason = (x.get("lost_reason") or "").strip()
        if lost and len(reason) < 2:
            raise AppError("reason_required", "Say why items are missing or damaged", 422)
        lots = _in_transit_lots(db, sl)
        if received + lost > sum(r.qty for r in lots):
            raise AppError("over_receipt", "More than is still in transit for this line", 409,
                           {"shipment_line_id": sl.id, "in_transit": sum(r.qty for r in lots),
                            "requested": received + lost})
        for qty, mtype, to, note in ((received, "transfer_receive", "US_MAIN", f"美國簽收 包裹 {sh.shipment_no}"),
                                     (lost, "transfer_loss", "LOSS", f"運送短少／損壞：{reason}（包裹 {sh.shipment_no}）")):
            remaining = qty
            for r in lots:  # oldest lot first; live balance, since receiving may already have used part of it
                if remaining == 0:
                    break
                take = min(_left(db, r.lot_id), remaining)
                if take <= 0:
                    continue
                post_movement(db, sku_id=sl.sku_id, lot_id=r.lot_id, movement_type=mtype, from_code="IN_TRANSIT",
                              to_code=to, qty=take, key=f"{key}:{mtype}:{sl.id}:{r.lot_id}",
                              ref_type="shipment_line", ref_id=sl.id, actor_id=actor_id, note=note, occurred_at=when)
                remaining -= take
        sl.qty_received += received
        sl.qty_lost += lost
        if lost:
            sl.discrepancy_reason = "；".join(x for x in (sl.discrepancy_reason, reason) if x)[:1000]
        touched += 1
    if not touched:
        raise AppError("nothing_to_receive", "Enter a received or missing quantity", 422)
    db.flush()
    left = db.scalar(text("SELECT COALESCE(sum(qty_shipped - qty_received - qty_lost), 0) FROM shipment_lines "
                          "WHERE shipment_id = :s"), {"s": sh.id})
    if left == 0:
        sh.status, sh.received_at = "received", when
    _event(db, sh.id, "received" if left == 0 else "partially_received", actor_id,
           {"lines": [{k: v for k, v in x.items() if k != "lost_reason"} for x in lines], "still_in_transit": int(left)})
    audit.record(db, action="parcel.received", entity_type="shipment", entity_id=sh.id, actor_id=actor_id,
                 after={"lines": lines, "still_in_transit": int(left), "received_on": str(when.date())}, ctx=ctx)
    db.flush()
    return sh


def _left(db: Session, lot_id: int) -> int:
    return db.scalar(text("SELECT COALESCE(sum(qty), 0) FROM inventory_balances WHERE lot_id = :l AND location_id = :t"),
                     {"l": lot_id, "t": location(db, "IN_TRANSIT").id}) or 0


# ---------- read side ----------

PARCEL_SQL = """
    SELECT s.id, s.shipment_no, s.status, s.tracking_no, s.shipped_at, s.eta, s.received_at, s.packages, s.note,
           c.id AS carrier_id, c.name AS carrier, c.tracking_url_template,
           COALESCE(sum(sl.qty_shipped), 0)::int AS units, COALESCE(sum(sl.qty_received), 0)::int AS received,
           COALESCE(sum(sl.qty_lost), 0)::int AS lost, count(sl.id)::int AS lines,
           COALESCE(sum((sl.qty_shipped - sl.qty_received - sl.qty_lost) * pl.landed_unit_cost_base), 0) AS value_in_transit
      FROM shipments s
      LEFT JOIN carriers c ON c.id = s.carrier_id
      LEFT JOIN shipment_lines sl ON sl.shipment_id = s.id
      LEFT JOIN purchase_lines pl ON pl.id = sl.purchase_line_id
     WHERE s.from_location_id = :supplier {where}
     GROUP BY s.id, c.id
"""


def _parcel_out(r, today: date, with_cost: bool) -> dict:
    d = dict(r)
    d["in_transit"] = d["units"] - d["received"] - d["lost"]
    d["tracking_url"] = (d.pop("tracking_url_template").replace("{n}", d["tracking_no"])
                         if d.get("tracking_url_template") and d.get("tracking_no") else None)
    d.pop("tracking_url_template", None)
    d["overdue_days"] = ((today - d["eta"]).days if d["status"] in ("shipped", "delivered") and d["eta"]
                         and d["eta"] < today else 0)
    d["days_in_transit"] = ((d["received_at"] or datetime.now(UTC)) - d["shipped_at"]).days if d["shipped_at"] else None
    d["value_in_transit"] = float(round(d["value_in_transit"], 2)) if with_cost else None
    return d


def list_parcels(db: Session, *, status: str = "open", with_cost: bool = False, limit: int = 200) -> list[dict]:
    where = {"open": "AND s.status IN ('shipped', 'delivered')", "received": "AND s.status = 'received'",
             "all": ""}.get(status, "")
    rows = db.execute(text(PARCEL_SQL.format(where=where) + " ORDER BY s.eta NULLS LAST, s.id DESC LIMIT :lim"),
                      {"supplier": location(db, "SUPPLIER").id, "lim": limit}).mappings().all()
    today = ledger.biz_date(datetime.now(UTC))
    return [_parcel_out(r, today, with_cost) for r in rows]


def parcel_detail(db: Session, shipment_id: int, with_cost: bool = False) -> dict:
    r = db.execute(text(PARCEL_SQL.format(where="AND s.id = :id")),
                   {"supplier": location(db, "SUPPLIER").id, "id": shipment_id}).mappings().first()
    if r is None:
        raise AppError("not_found", "Parcel not found", 404)
    out = _parcel_out(r, ledger.biz_date(datetime.now(UTC)), with_cost)
    out["lines"] = [dict(x) | ({"unit_cost": float(x["unit_cost"])} if with_cost else {"unit_cost": None})
                    for x in db.execute(text("""
        SELECT sl.id, sl.sku_id, s.sku_code, s.name, sl.purchase_line_id, b.batch_no, sl.qty_shipped, sl.qty_received,
               sl.qty_lost, sl.qty_shipped - sl.qty_received - sl.qty_lost AS in_transit, sl.discrepancy_reason,
               pl.landed_unit_cost_base AS unit_cost
          FROM shipment_lines sl JOIN skus s ON s.id = sl.sku_id
          LEFT JOIN purchase_lines pl ON pl.id = sl.purchase_line_id
          LEFT JOIN purchase_batches b ON b.id = pl.batch_id
         WHERE sl.shipment_id = :id ORDER BY s.sku_code"""), {"id": shipment_id}).mappings()]
    out["events"] = [dict(e) for e in db.execute(text("""
        SELECT e.event, e.occurred_at, e.data, u.display_name AS user_name FROM shipment_events e
          LEFT JOIN users u ON u.id = e.user_id WHERE e.shipment_id = :id ORDER BY e.id"""), {"id": shipment_id}).mappings()]
    return out


def lead_time_stats(db: Session, days: int = 180) -> dict:
    """Actual shipped -> fully received, in days, for parcels received in the last `days` days."""
    rows = [r[0] for r in db.execute(text("""
        SELECT EXTRACT(EPOCH FROM (received_at - shipped_at)) / 86400 FROM shipments
         WHERE status = 'received' AND received_at >= now() - make_interval(days => :d) AND shipped_at IS NOT NULL
         ORDER BY 1"""), {"d": days})]
    if not rows:
        return {"parcels": 0, "average_days": None, "median_days": None, "max_days": None,
                "planned_days": purchasing.lead_time_days(db)}
    vals = [float(v) for v in rows]
    mid = len(vals) // 2
    median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return {"parcels": len(vals), "average_days": round(sum(vals) / len(vals), 1), "median_days": round(median, 1),
            "max_days": round(max(vals), 1), "planned_days": purchasing.lead_time_days(db)}


def outbound_backlog(db: Session, older_than_days: int = 2, limit: int = 300) -> dict:
    """Paid Whatnot orders that have not left the US warehouse yet, judged by Whatnot's own shipment status
    (so it is right even for lines not mapped to a SKU yet)."""
    base = """
        FROM sales_orders o
        WHERE o.platform = 'whatnot' AND o.status = 'pending_fulfillment'
          AND NOT EXISTS (SELECT 1 FROM sales_order_lines x JOIN sales_shipments sh ON sh.external_id = x.external_shipment_id
                           WHERE x.order_id = o.id AND sh.status IN ('dropped_off', 'in_transit', 'delivered'))"""
    totals = db.execute(text(f"""SELECT count(*) AS orders,
                                        count(*) FILTER (WHERE o.ordered_at < now() - make_interval(days => :d)) AS late,
                                        COALESCE(sum((SELECT sum(qty) FROM sales_order_lines x WHERE x.order_id = o.id)), 0) AS units
                                 {base}"""), {"d": older_than_days}).one()
    rows = db.execute(text(f"""
        SELECT o.id, o.external_order_id, o.ordered_at, o.status, l.title AS livestream_title,
               (SELECT COALESCE(sum(qty), 0) FROM sales_order_lines x WHERE x.order_id = o.id)::int AS units,
               (SELECT max(sh.status) FROM sales_order_lines x JOIN sales_shipments sh ON sh.external_id = x.external_shipment_id
                 WHERE x.order_id = o.id) AS shipment_status,
               EXISTS (SELECT 1 FROM sales_order_lines x WHERE x.order_id = o.id AND x.exception_code IS NOT NULL) AS has_issue,
               EXTRACT(EPOCH FROM (now() - o.ordered_at)) / 86400 AS age_days
          {base.replace("FROM sales_orders o", "FROM sales_orders o LEFT JOIN livestreams l ON l.id = o.livestream_id")}
         ORDER BY o.ordered_at
         LIMIT :lim"""), {"lim": limit}).mappings().all()
    items = [dict(r) | {"age_days": round(float(r["age_days"]), 1)} for r in rows]
    return {"older_than_days": older_than_days, "orders": totals.orders, "late_orders": totals.late,
            "units": int(totals.units), "items": items, "shown": len(items)}
