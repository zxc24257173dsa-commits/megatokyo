"""/api/v1 logistics (Phase 9): inbound parcels Japan -> US, lead time, US outbound backlog."""
from datetime import date

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_active_user, get_request_context, require_permissions
from app.core.errors import AppError
from app.db.session import get_db
from app.models.logistics import Carrier
from app.services import logistics as L
from app.services.audit import RequestContext
from app.services.inventory_queries import can_view_cost

router = APIRouter(tags=["logistics"])
view = require_permissions("inventory.view")
ship = require_permissions("shipment.manage_jp")
receive = require_permissions("shipment.receive_us")
IdemKey = Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


@router.get("/carriers")
def carriers(current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return [{"id": c.id, "name": c.name, "has_tracking_link": bool(c.tracking_url_template)}
            for c in db.scalars(select(Carrier).order_by(Carrier.id))]


@router.get("/parcels")
def parcels(status: str = "open", current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return L.list_parcels(db, status=status, with_cost=can_view_cost(set(current.permissions)))


@router.get("/parcels/{shipment_id}")
def parcel(shipment_id: int, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return L.parcel_detail(db, shipment_id, with_cost=can_view_cost(set(current.permissions)))


class ParcelLineIn(BaseModel):
    purchase_line_id: int
    qty: int = Field(ge=1, le=100000)


class ParcelIn(BaseModel):
    lines: list[ParcelLineIn] = Field(min_length=1, max_length=200)
    shipped_on: date | None = None
    carrier_id: int | None = None
    tracking_no: str | None = Field(default=None, max_length=128)
    eta: date | None = None
    packages: int | None = Field(default=None, ge=1, le=1000)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/parcels", status_code=201)
def create_parcel(body: ParcelIn, idempotency_key: str = IdemKey, current: CurrentUser = Depends(ship),
                  db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    sh = L.create_parcel(db, actor_id=current.id, lines=[x.model_dump() for x in body.lines],
                         shipped_on=body.shipped_on, carrier_id=body.carrier_id, tracking_no=body.tracking_no,
                         eta=body.eta, packages=body.packages, note=body.note,
                         idempotency_key=f"{current.id}:{idempotency_key}", ctx=ctx)
    db.commit()
    return L.parcel_detail(db, sh.id, with_cost=can_view_cost(set(current.permissions)))


class ParcelUpdateIn(BaseModel):
    carrier_id: int | None = None
    tracking_no: str | None = Field(default=None, max_length=128)
    eta: date | None = None
    packages: int | None = Field(default=None, ge=1, le=1000)
    note: str | None = Field(default=None, max_length=2000)


def _ship_or_receive(current: CurrentUser = Depends(get_active_user)) -> CurrentUser:
    if not {"shipment.manage_jp", "shipment.receive_us"} & set(current.permissions):
        raise AppError("forbidden", "You do not have permission for this action", 403,
                       {"missing": ["shipment.manage_jp"]})
    return current


@router.patch("/parcels/{shipment_id}")
def update_parcel(shipment_id: int, body: ParcelUpdateIn, current: CurrentUser = Depends(_ship_or_receive),
                  db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    L.update_parcel(db, actor_id=current.id, shipment_id=shipment_id, tracking_no=body.tracking_no,
                    carrier_id=body.carrier_id, eta=body.eta, packages=body.packages, note=body.note, ctx=ctx)
    db.commit()
    return L.parcel_detail(db, shipment_id, with_cost=can_view_cost(set(current.permissions)))


class ReceiveLineIn(BaseModel):
    shipment_line_id: int
    received: int = Field(default=0, ge=0, le=100000)
    lost: int = Field(default=0, ge=0, le=100000)
    lost_reason: str | None = Field(default=None, max_length=500)


class ReceiveIn(BaseModel):
    lines: list[ReceiveLineIn] = Field(min_length=1, max_length=200)
    received_on: date | None = None


@router.post("/parcels/{shipment_id}/receive")
def receive_parcel(shipment_id: int, body: ReceiveIn, idempotency_key: str = IdemKey,
                   current: CurrentUser = Depends(receive), db: Session = Depends(get_db),
                   ctx: RequestContext = Depends(get_request_context)):
    L.receive_parcel(db, actor_id=current.id, shipment_id=shipment_id, lines=[x.model_dump() for x in body.lines],
                     received_on=body.received_on, idempotency_key=f"{current.id}:{idempotency_key}", ctx=ctx)
    db.commit()
    return L.parcel_detail(db, shipment_id, with_cost=can_view_cost(set(current.permissions)))


@router.get("/logistics/lead-time")
def lead_time(days: int = 180, current: CurrentUser = Depends(view), db: Session = Depends(get_db)):
    return L.lead_time_stats(db, days)


@router.get("/logistics/outbound-backlog")
def outbound(older_than_days: int = 2, current: CurrentUser = Depends(require_permissions("orders.view")),
             db: Session = Depends(get_db)):
    return L.outbound_backlog(db, older_than_days)
