"""Replenishment suggestions (Phase 10). Advisory only: nothing is ever ordered automatically.

Owner's flow: buy in Japan, ship straight to the US warehouse; order -> sellable in the US takes 9-11 days,
we plan with the conservative end (supplier_lead_time_days, default 11). No fixed order day: suggestions
cover one review period (default 7 days) beyond the lead time.

For every active, stocked SKU (bundles have no stock of their own - their sales are demand on their
components), with data up to the last COMPLETE business day:

  demand / day    v7, v14, v30 = average daily stock demand (Phase 8: bundles expanded, giveaways included)
                  v = 0.5 v7 + 0.3 v14 + 0.2 v30   (weights configurable)
                  fewer than `min_history_days` of history -> the SKU's manual velocity if set, else flagged
  volatility      sigma = standard deviation of daily demand over the last 30 days
  safety stock    SS = ceil(z * sigma * sqrt(L))     z = 1.65 (~95%), L = lead time; or the SKU's override
  target          S = v * (L + R) + SS               R = review period
  position        IP = US available (on hand - reserved) + in transit + purchased but not shipped yet
                  (in-transit stock is never counted as sellable; it only counts as arriving supply)
  buy             max(0, S - IP), rounded up to the case pack, at least the minimum quantity

Cards obtained by opening boxes (they are box contents) are not bought: their shortfall becomes
"open N boxes", and their demand is added to the box's demand (per box, the busiest content decides),
so the box gets bought in time. Past box openings are therefore not counted again as box demand.

Urgency (latest order date = the day the position minus safety stock runs out, minus the lead time):
  red     already past / today, or out of US stock while selling
  orange  within 3 days     yellow  within the review period     green  later, or no demand
"""
import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.replenishment import (
    ReplenishmentRun, ReplenishmentSettings, ReplenishmentSuggestion, SkuReplenishmentParams,
)
from app.services import audit, ledger
from app.services.audit import RequestContext
from app.services.inventory import location
from app.services.trends import daily_demand

URGENT_DAYS = 3


def _note(key: str, **args) -> dict:
    """An explanation note the screen translates: the Chinese template is the key (see frontend locales/en.ts)."""
    return {"key": key, "args": args}


def settings(db: Session) -> ReplenishmentSettings:
    s = db.get(ReplenishmentSettings, 1)
    if s is None:
        s = ReplenishmentSettings(id=1)
        db.add(s)
        db.flush()
    return s


def _mean(vals: list[int]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: list[int]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def _round_up(qty: float, pack: int, minimum: int | None) -> int:
    if qty <= 0:
        return 0
    q = math.ceil(qty - 1e-9)
    q = max(q, minimum or 0)
    return math.ceil(q / pack) * pack


def _positions(db: Session) -> dict[int, dict]:
    transit = location(db, "IN_TRANSIT").id
    rows = db.execute(text("""
        SELECT s.id AS sku_id, s.sku_code, s.name, s.is_bundle, s.status, s.safety_stock_override, s.created_at,
               COALESCE(v.us_available, 0) AS us_available, COALESCE(v.us_reserved, 0) AS us_reserved,
               COALESCE(v.in_transit, 0) AS in_transit
          FROM skus s LEFT JOIN v_inventory_position v ON v.sku_id = s.id""")).mappings().all()
    out = {r["sku_id"]: dict(r) for r in rows}
    # in-transit by expected arrival (parcel ETA; lots shipped before parcels: ship date + planned transit)
    for r in db.execute(text("""
        SELECT l.sku_id, COALESCE(sh.eta, (l.received_at AT TIME ZONE :tz)::date + :lead) AS eta, sum(b.qty) AS qty
          FROM inventory_balances b JOIN inventory_lots l ON l.id = b.lot_id
          LEFT JOIN shipment_lines sl ON sl.id = l.shipment_line_id LEFT JOIN shipments sh ON sh.id = sl.shipment_id
         WHERE b.location_id = :t AND b.qty > 0 GROUP BY 1, 2"""),
            {"t": transit, "tz": ledger_tz(), "lead": settings(db).lead_time_days}):
        if r.sku_id in out:
            out[r.sku_id].setdefault("transit_by_eta", []).append((r.eta, int(r.qty)))
    # purchased but not shipped from Japan yet
    for r in db.execute(text("""
        SELECT pl.sku_id, sum(pl.qty - COALESCE((SELECT sum(l.original_qty) FROM inventory_lots l
                                                  WHERE l.purchase_line_id = pl.id), 0)) AS open_qty
          FROM purchase_lines pl GROUP BY 1""")):
        if r.sku_id in out:
            out[r.sku_id]["on_order"] = max(0, int(r.open_qty or 0))
    return out


def ledger_tz() -> str:
    from app.core.config import get_settings
    return get_settings().business_timezone


def compute(db: Session) -> dict:
    """Suggestions for every SKU, not saved (see `create_run`)."""
    st = settings(db)
    today = ledger.biz_date(datetime.now(UTC))
    as_of = today - timedelta(days=1)  # last complete business day
    start = as_of - timedelta(days=29)
    demand = daily_demand(db, start, as_of, include_box_openings=False)
    pos = _positions(db)
    params = {p.sku_id: p for p in db.scalars(select(SkuReplenishmentParams))}
    boxes: dict[int, list[tuple[int, int]]] = defaultdict(list)  # box -> [(content, qty per box)]
    box_of: dict[int, tuple[int, int]] = {}  # content -> (box, qty per box); first box wins
    for r in db.execute(text("SELECT box_sku_id, content_sku_id, qty FROM sku_box_contents ORDER BY box_sku_id")):
        boxes[r.box_sku_id].append((r.content_sku_id, r.qty))
        box_of.setdefault(r.content_sku_id, (r.box_sku_id, r.qty))
    first_sale = dict(db.execute(text("""
        SELECT l.sku_id, min((o.paid_at AT TIME ZONE :tz)::date) FROM sales_order_lines l
          JOIN sales_orders o ON o.id = l.order_id WHERE l.sku_id IS NOT NULL
           AND o.status IN ('pending_fulfillment', 'completed') GROUP BY 1"""), {"tz": ledger_tz()}).all())
    w7, w14, w30 = float(st.weight_7d), float(st.weight_14d), float(st.weight_30d)
    z, R = float(st.service_level_z), st.review_period_days

    def series(sku: int) -> list[int]:
        d = demand.get(sku, {})
        return [d.get(start + timedelta(days=i), 0) for i in range(30)]

    def velocity(sku: int) -> tuple[float, float, float, float, float, list[str]]:
        s = series(sku)
        v7, v14, v30 = _mean(s[-7:]), _mean(s[-14:]), _mean(s)
        notes = []
        fs = first_sale.get(sku)
        history = (as_of - fs).days + 1 if fs else 0
        p = params.get(sku)
        if history < st.min_history_days:
            if p and p.manual_daily_velocity is not None:
                notes.append(_note("銷售紀錄只有 {days} 天，使用手動設定的每日銷量 {v}", days=history,
                                   v=f"{float(p.manual_daily_velocity):g}"))
                v = float(p.manual_daily_velocity)
                return v7, v14, v30, v, _std(s), notes
            if history:
                notes.append(_note("銷售紀錄只有 {days} 天（少於 {min} 天），需求可能不準", days=history,
                                   min=st.min_history_days))
                # only average over days it was actually on sale
                v = _mean(s[-history:]) if history < 30 else w7 * v7 + w14 * v14 + w30 * v30
                return v7, v14, v30, v, _std(s[-history:]), notes
        return v7, v14, v30, w7 * v7 + w14 * v14 + w30 * v30, _std(s), notes

    results: dict[int, dict] = {}
    # 1) everything except boxes-with-contents, which need the contents' numbers first
    order = sorted(pos, key=lambda k: (k in boxes, k))
    for sku in order:
        p_ = pos[sku]
        prm = params.get(sku)
        if p_["is_bundle"] or p_["status"] != "active" or (prm and prm.excluded):
            continue
        L = (prm.supplier_lead_time_days if prm and prm.supplier_lead_time_days else st.supplier_lead_time_days)
        v7, v14, v30, v, sigma, notes = velocity(sku)
        extra_box_rate = 0.0
        if sku in boxes:  # demand that will come from opening this box for its contents
            rates = [(results[c]["v_blended"] / q, c, q) for c, q in boxes[sku] if c in results and q]
            if rates:
                rate, c, q = max(rates)
                if rate > 0:
                    extra_box_rate = rate
                    notes.append(_note("另外每天約 {rate} 盒要拆給盒內商品（主要是 {sku}，每盒 {q} 件）",
                                       rate=f"{rate:.2f}", sku=results[c]["sku_code"], q=q))
                    sigma = math.sqrt(sigma ** 2 + (results[c]["sigma_daily"] / q) ** 2)
        v_total = v + extra_box_rate
        ss_override = p_["safety_stock_override"] if p_["safety_stock_override"] is not None else (
            prm.safety_stock_override if prm else None)
        ss = ss_override if ss_override is not None else math.ceil(z * sigma * math.sqrt(L) - 1e-9)
        if ss_override is not None:
            notes.append(_note("安全庫存使用手動設定 {n}", n=ss_override))
        target = v_total * (L + R) + ss
        horizon = today + timedelta(days=L)
        tr = p_.get("transit_by_eta", [])
        in_time = sum(q for eta, q in tr if eta <= horizon)
        late = sum(q for eta, q in tr if eta > horizon)
        on_order = p_.get("on_order", 0)
        avail = int(p_["us_available"])
        ip = avail + in_time + late + on_order
        shortfall = target - ip
        days_to_out = (avail / v_total) if v_total > 0 else None
        stockout = today + timedelta(days=math.floor(days_to_out)) if days_to_out is not None else None
        cover_days = ((ip - ss) / v_total) if v_total > 0 else None
        latest = (today + timedelta(days=math.floor(cover_days) - L)) if cover_days is not None else None
        action, buy, open_n, box_id = "none", 0, 0, None
        if sku in box_of:  # a card that comes out of a box: open boxes instead of buying it
            box_id, per_box = box_of[sku]
            if shortfall > 0:
                action = "open_box"
                open_n = math.ceil(shortfall / per_box - 1e-9)
        elif shortfall > 0:
            action = "buy"
            buy = _round_up(shortfall, prm.case_pack if prm else 1, prm.min_ship_qty if prm else None)
        if v_total <= 0:
            color = "green" if avail > 0 or action == "none" else "yellow"
        elif latest is None or latest <= today or (avail <= 0):
            color = "red"
        elif (latest - today).days <= URGENT_DAYS:
            color = "orange"
        elif (latest - today).days <= R:
            color = "yellow"
        else:
            color = "green"
        if action == "none" and color in ("red", "orange") and v_total > 0 and ip >= target:
            color = "yellow"  # stock is coming in time; just watch it
        results[sku] = {
            "sku_id": sku, "sku_code": p_["sku_code"], "name": p_["name"],
            "v7": round(v7, 4), "v14": round(v14, 4), "v30": round(v30, 4), "v_blended": round(v_total, 4),
            "sigma_daily": round(sigma, 4), "lead_days": L, "review_days": R, "horizon_days": L + R,
            "forecast_demand": round(v_total * (L + R), 2), "safety_stock": int(ss), "target": round(target, 2),
            "us_available": avail, "us_reserved": int(p_["us_reserved"]), "in_transit_in_time": in_time,
            "in_transit_late": late, "on_order": on_order, "position": ip,
            "days_to_stockout": round(days_to_out, 1) if days_to_out is not None else None,
            "stockout_date": stockout, "latest_order_date": latest, "action": action, "suggested_purchase_qty": buy,
            "boxes_to_open": open_n, "source_box_sku_id": box_id, "status_color": color, "notes": notes,
            "v_direct": round(v, 4), "box_open_rate": round(extra_box_rate, 4),
        }
    # 2) a card's "open N boxes" can't exceed the boxes actually in the US
    for r in results.values():
        if r["action"] == "open_box" and r["source_box_sku_id"] in pos:
            have = int(pos[r["source_box_sku_id"]]["us_available"])
            if r["boxes_to_open"] > have:
                r["notes"].append(_note("美國只有 {n} 盒可拆；其餘要等盒子到貨（盒子的建議已包含這部分需求）", n=have))
                r["boxes_to_open"] = have
    rank = {"red": 0, "orange": 1, "yellow": 2, "green": 3}
    rows = sorted(results.values(), key=lambda r: (rank[r["status_color"]], r["latest_order_date"] or date.max,
                                                    -r["v_blended"]))
    return {"as_of": as_of, "today": today, "settings": settings_out(st), "suggestions": rows}


def settings_out(st: ReplenishmentSettings) -> dict:
    return {"supplier_lead_time_days": st.supplier_lead_time_days, "transit_days": st.lead_time_days,
            "review_period_days": st.review_period_days, "service_level_z": float(st.service_level_z),
            "weights": [float(st.weight_7d), float(st.weight_14d), float(st.weight_30d)],
            "min_history_days": st.min_history_days}


def create_run(db: Session, *, actor_id: int, note: str | None, ctx: RequestContext) -> ReplenishmentRun:
    data = compute(db)
    run = ReplenishmentRun(data_as_of=data["as_of"], params_snapshot=data["settings"], status="completed",
                           created_by=actor_id, note=(note or "").strip() or None)
    db.add(run)
    db.flush()
    for r in data["suggestions"]:
        db.add(ReplenishmentSuggestion(
            run_id=run.id, sku_id=r["sku_id"], v7=r["v7"], v14=r["v14"], v30=r["v30"], v_blended=r["v_blended"],
            sigma_daily=r["sigma_daily"], horizon_days=r["horizon_days"], forecast_demand=r["forecast_demand"],
            safety_stock=r["safety_stock"], committed_demand=r["us_reserved"], us_available=r["us_available"],
            in_transit_in_time=r["in_transit_in_time"], in_transit_late=r["in_transit_late"], jp_available=0,
            on_order=r["on_order"], days_to_stockout=r["days_to_stockout"], stockout_date=r["stockout_date"],
            latest_ship_date=None, latest_order_date=r["latest_order_date"], suggested_ship_qty=0,
            suggested_purchase_qty=r["suggested_purchase_qty"], boxes_to_open=r["boxes_to_open"],
            source_box_sku_id=r["source_box_sku_id"], action=r["action"], status_color=r["status_color"],
            explanation={"notes": r["notes"], "target": r["target"], "position": r["position"],
                         "lead_days": r["lead_days"], "review_days": r["review_days"],
                         "v_direct": r["v_direct"], "box_open_rate": r["box_open_rate"]}))
    db.flush()
    audit.record(db, action="replenishment.run", entity_type="replenishment_run", entity_id=run.id, actor_id=actor_id,
                 after={"as_of": str(data["as_of"]), "skus": len(data["suggestions"]),
                        "to_buy": sum(1 for r in data["suggestions"] if r["action"] == "buy")}, ctx=ctx)
    return run


def run_detail(db: Session, run_id: int | None = None) -> dict:
    run = (db.get(ReplenishmentRun, run_id) if run_id else
           db.scalar(select(ReplenishmentRun).order_by(ReplenishmentRun.id.desc()).limit(1)))
    if run is None:
        if run_id:
            raise AppError("not_found", "Replenishment run not found", 404)
        return {"run": None, "suggestions": []}
    rows = db.execute(text("""
        SELECT r.*, s.sku_code, s.name, b.sku_code AS box_sku_code, u.display_name AS confirmed_by_name,
               COALESCE(r.override_purchase_qty, r.suggested_purchase_qty) AS final_qty
          FROM replenishment_suggestions r JOIN skus s ON s.id = r.sku_id
          LEFT JOIN skus b ON b.id = r.source_box_sku_id
          LEFT JOIN users u ON u.id = r.confirmed_by
         WHERE r.run_id = :r
         ORDER BY CASE r.status_color WHEN 'red' THEN 0 WHEN 'orange' THEN 1 WHEN 'yellow' THEN 2 ELSE 3 END,
                  r.latest_order_date NULLS LAST, r.v_blended DESC"""), {"r": run.id}).mappings().all()
    out = []
    for r in rows:
        d = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in dict(r).items()}
        out.append(d)
    return {"run": {"id": run.id, "data_as_of": run.data_as_of, "created_at": run.created_at, "note": run.note,
                    "params": run.params_snapshot},
            "suggestions": out}


def override(db: Session, *, actor_id: int, suggestion_id: int, qty: int | None, reason: str | None,
             ctx: RequestContext) -> ReplenishmentSuggestion:
    s = db.get(ReplenishmentSuggestion, suggestion_id, with_for_update=True)
    if s is None:
        raise AppError("not_found", "Suggestion not found", 404)
    if s.confirmed_at is not None:
        raise AppError("already_confirmed", "This suggestion is already confirmed", 409)
    if qty is not None:
        if qty < 0:
            raise AppError("invalid_qty", "Quantity cannot be negative", 422)
        if not reason or len(reason.strip()) < 2:
            raise AppError("reason_required", "Give a reason for changing the suggestion", 422)
    before = {"override": s.override_purchase_qty, "reason": s.override_reason}
    s.override_purchase_qty = qty
    s.override_reason = reason.strip()[:500] if qty is not None else None
    s.overridden_by = actor_id if qty is not None else None
    db.flush()
    audit.record(db, action="replenishment.override", entity_type="replenishment_suggestion", entity_id=s.id,
                 actor_id=actor_id, before=before, after={"override": qty, "reason": s.override_reason}, ctx=ctx)
    return s


def confirm(db: Session, *, actor_id: int, run_id: int, suggestion_ids: list[int], ctx: RequestContext) -> int:
    """Record that a person decided to buy these. It does NOT place any order with anyone."""
    n = 0
    for sid in suggestion_ids:
        s = db.get(ReplenishmentSuggestion, sid, with_for_update=True)
        if s is None or s.run_id != run_id:
            raise AppError("not_found", f"Suggestion {sid} not in this run", 404)
        if s.confirmed_at is None:
            s.confirmed_by, s.confirmed_at = actor_id, datetime.now(UTC)
            n += 1
    db.flush()
    audit.record(db, action="replenishment.confirmed", entity_type="replenishment_run", entity_id=run_id,
                 actor_id=actor_id, after={"suggestions": suggestion_ids}, ctx=ctx)
    return n


def update_settings(db: Session, *, actor_id: int, values: dict, ctx: RequestContext) -> ReplenishmentSettings:
    st = settings(db)
    before = settings_out(st)
    for k in ("supplier_lead_time_days", "review_period_days", "min_history_days"):
        if values.get(k) is not None:
            setattr(st, k, int(values[k]))
    if values.get("service_level_z") is not None:
        st.service_level_z = Decimal(str(values["service_level_z"]))
    if values.get("weights") is not None:
        w = [Decimal(str(x)) for x in values["weights"]]
        if len(w) != 3 or sum(w) != 1 or any(x < 0 for x in w):
            raise AppError("invalid_weights", "The three weights must be >= 0 and add up to 1", 422)
        st.weight_7d, st.weight_14d, st.weight_30d = w
    st.updated_by, st.updated_at = actor_id, datetime.now(UTC)
    db.flush()
    audit.record(db, action="replenishment.settings", entity_type="replenishment_settings", entity_id=1,
                 actor_id=actor_id, before=before, after=settings_out(st), ctx=ctx)
    return st


def set_sku_params(db: Session, *, actor_id: int, sku_id: int, values: dict, ctx: RequestContext) -> SkuReplenishmentParams:
    p = db.get(SkuReplenishmentParams, sku_id) or SkuReplenishmentParams(sku_id=sku_id)
    db.add(p)
    for k in ("excluded", "case_pack", "min_ship_qty", "supplier_lead_time_days", "manual_daily_velocity",
              "safety_stock_override"):
        if k in values:
            setattr(p, k, values[k])
    db.flush()
    audit.record(db, action="replenishment.sku_params", entity_type="sku", entity_id=sku_id, actor_id=actor_id,
                 after={k: str(v) for k, v in values.items()}, ctx=ctx)
    return p
