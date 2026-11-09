"""/api/v1/imports (CSV / XLSX upload, mapping, preview, commit) and /api/v1/purchase-batches."""
from datetime import date

from fastapi import APIRouter, Depends, File, Form, Header, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_active_user, get_request_context, require_permissions
from app.core.errors import AppError
from app.db.session import get_db
from app.importers import engine, mapping
from app.models.imports import ImportJob, ImportRow
from app.services import inventory_queries as q
from app.services import purchasing
from app.services.audit import RequestContext

router = APIRouter(tags=["imports"])
TYPE_PERMISSION = {"purchase_cost": "import.cost", "sku_master": "catalog.edit"}


def _check_type_permission(import_type: str, current: CurrentUser) -> None:
    perm = TYPE_PERMISSION.get(import_type)
    if perm is None:
        raise AppError("unsupported_import_type", f"Unsupported import type {import_type}", 422)
    if perm not in current.permissions:
        raise AppError("forbidden", "You do not have permission for this import", 403, {"missing": [perm]})


def _job(db: Session, job_id: int, current: CurrentUser, *, for_update: bool = False) -> ImportJob:
    job = db.get(ImportJob, job_id, with_for_update=for_update, populate_existing=for_update)
    if job is None:
        raise AppError("not_found", "Import not found", 404)
    _check_type_permission(job.import_type, current)
    return job


def _job_out(db: Session, job: ImportJob, *, include_preview: bool = True) -> dict:
    cm = job.column_mapping or {}
    fields = [{"key": f.key, "label": f.label, "required": f.required} for f in mapping.FIELDS[job.import_type]]
    headers = []
    if cm.get("sheet") is not None:
        sheet = next((s for s in job.sheets if s["name"] == cm["sheet"]), None)
        if sheet and cm["header_row"] < len(sheet["preview"]):
            headers = sheet["preview"][cm["header_row"]]
    return {
        "id": job.id, "import_type": job.import_type, "filename": job.filename, "status": job.status,
        "created_at": job.created_at, "committed_at": job.committed_at, "stats": job.stats,
        "mapping": cm, "fields": fields, "headers": headers,
        "sheets": job.sheets if include_preview else [{"name": s["name"], "rows": s["rows"]} for s in job.sheets],
    }


@router.post("/imports", status_code=201)
async def upload(import_type: str = Form(...), file: UploadFile = File(...),
                 current: CurrentUser = Depends(get_active_user), db: Session = Depends(get_db),
                 ctx: RequestContext = Depends(get_request_context)):
    _check_type_permission(import_type, current)
    data = await file.read(engine.MAX_FILE_BYTES + 1)
    job = engine.create_job(db, actor_id=current.id, import_type=import_type, filename=file.filename or "upload",
                            data=data, ctx=ctx)
    db.commit()
    return _job_out(db, job)


@router.get("/imports")
def list_imports(current: CurrentUser = Depends(get_active_user), db: Session = Depends(get_db)):
    allowed = [t for t, p in TYPE_PERMISSION.items() if p in current.permissions]
    jobs = db.scalars(select(ImportJob).where(ImportJob.import_type.in_(allowed))
                      .order_by(ImportJob.id.desc()).limit(100))
    return [_job_out(db, j, include_preview=False) for j in jobs]


@router.get("/imports/{job_id}")
def get_import(job_id: int, current: CurrentUser = Depends(get_active_user), db: Session = Depends(get_db)):
    return _job_out(db, _job(db, job_id, current))


@router.get("/imports/{job_id}/rows")
def import_rows(job_id: int, filter: str = Query(default="all", pattern="^(all|errors|warnings|import|skip)$"),
                current: CurrentUser = Depends(get_active_user), db: Session = Depends(get_db)):
    _job(db, job_id, current)
    rows = list(db.scalars(select(ImportRow).where(ImportRow.job_id == job_id).order_by(ImportRow.row_number)))
    pick = {
        "all": lambda r: True,
        "errors": lambda r: bool(r.errors),
        "warnings": lambda r: bool(r.warnings) and not r.errors and r.proposed_action != "skip_example",
        "import": lambda r: not r.errors and not (r.proposed_action or "").startswith("skip")
        and r.proposed_action != "unchanged",
        "skip": lambda r: (r.proposed_action or "").startswith("skip") or r.proposed_action == "unchanged",
    }[filter]
    return [{"row_number": r.row_number, "action": r.proposed_action, "errors": r.errors, "warnings": r.warnings,
             "normalized": {k: v for k, v in (r.normalized or {}).items() if not k.startswith("_")},
             "raw": r.raw, "matched_sku_id": r.matched_sku_id} for r in rows if pick(r)][:2000]


class MappingIn(BaseModel):
    sheet: str
    header_row: int = Field(ge=0)
    columns: dict[str, int | None]


@router.put("/imports/{job_id}/mapping")
def set_mapping(job_id: int, body: MappingIn, current: CurrentUser = Depends(get_active_user),
                db: Session = Depends(get_db)):
    job = _job(db, job_id, current, for_update=True)
    engine.set_mapping(db, job=job, sheet=body.sheet, header_row=body.header_row, columns=body.columns)
    db.commit()
    return _job_out(db, job)


class CommitIn(BaseModel):
    create_missing_skus: bool = False
    skip_error_rows: bool = False


@router.post("/imports/{job_id}/commit")
def commit_import(job_id: int, body: CommitIn, current: CurrentUser = Depends(get_active_user),
                  db: Session = Depends(get_db), ctx: RequestContext = Depends(get_request_context)):
    job = _job(db, job_id, current, for_update=True)
    if job.import_type == "purchase_cost" and body.create_missing_skus and "catalog.edit" not in current.permissions:
        raise AppError("forbidden", "Creating new SKUs needs catalog.edit", 403, {"missing": ["catalog.edit"]})
    summary = engine.commit(db, actor_id=current.id, job=job, create_missing_skus=body.create_missing_skus,
                            skip_error_rows=body.skip_error_rows, ctx=ctx)
    db.commit()
    return {"job": _job_out(db, job, include_preview=False), "summary": summary}


@router.post("/imports/{job_id}/cancel")
def cancel_import(job_id: int, current: CurrentUser = Depends(get_active_user), db: Session = Depends(get_db),
                  ctx: RequestContext = Depends(get_request_context)):
    job = _job(db, job_id, current, for_update=True)
    engine.cancel(db, actor_id=current.id, job=job, ctx=ctx)
    db.commit()
    return _job_out(db, job, include_preview=False)


# ---------- purchase batches & receiving ----------

@router.get("/purchase-batches")
def list_batches(search: str | None = Query(default=None, max_length=100), only_open: bool = False,
                 current: CurrentUser = Depends(require_permissions("inventory.view")), db: Session = Depends(get_db)):
    return purchasing.list_batches(db, search=search, only_open=only_open,
                                   with_cost=q.can_view_cost(current.permissions))


@router.get("/purchase-batches/{batch_id}")
def get_batch(batch_id: int, current: CurrentUser = Depends(require_permissions("inventory.view")),
              db: Session = Depends(get_db)):
    return purchasing.batch_detail(db, batch_id, with_cost=q.can_view_cost(current.permissions))


class ReceiveIn(BaseModel):
    qty: int = Field(ge=1, le=1_000_000)
    received_on: date | None = None


@router.post("/purchase-lines/{line_id}/receive", status_code=201)
def receive(line_id: int, body: ReceiveIn,
            idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$"),
            current: CurrentUser = Depends(require_permissions("inventory.adjust_jp")), db: Session = Depends(get_db),
            ctx: RequestContext = Depends(get_request_context)):
    move = purchasing.receive_line(db, actor_id=current.id, line_id=line_id, qty=body.qty,
                                   received_on=body.received_on, idempotency_key=idempotency_key, ctx=ctx)
    db.commit()
    return {"movement_id": move.id, "received_total": purchasing.received_qty(db, line_id)}
