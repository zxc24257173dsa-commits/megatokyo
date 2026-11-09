"""Two-phase import workflow shared by every import type.

  upload  -> file stored, sheets read, sheet / header row / columns detected (or taken from a
             remembered layout), rows validated into import_rows      (status: validated)
  mapping -> user corrects sheet / header row / columns -> re-validated
  commit  -> rows validated AGAIN against the current database, then written in ONE
             transaction; the same file can never be committed twice  (status: committed)
  cancel  -> nothing is written                                     (status: cancelled)
"""
import hashlib
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.importers import mapping, normalize, purchase_cost, reader, sku_master
from app.models.imports import ImportJob, ImportMappingTemplate, ImportRow
from app.services import audit, storage
from app.services.audit import RequestContext

MAX_FILE_BYTES = 10 * 1024 * 1024
HANDLERS = {"purchase_cost": purchase_cost, "sku_master": sku_master}
PREVIEW_ROWS = 15


def _header_signature(headers: list) -> str:
    keys = "|".join(normalize.header_key(h) for h in headers)
    return hashlib.sha256(keys.encode()).hexdigest()


def _load_sheets(job: ImportJob) -> list[reader.Sheet]:
    return reader.read(job.filename, storage.path_for(job.storage_key).read_bytes())


def create_job(db: Session, *, actor_id: int, import_type: str, filename: str, data: bytes,
               ctx: RequestContext) -> ImportJob:
    if import_type not in HANDLERS:
        raise AppError("unsupported_import_type", f"Unsupported import type {import_type}", 422)
    if not data:
        raise AppError("empty_file", "The file is empty", 422)
    if len(data) > MAX_FILE_BYTES:
        raise AppError("file_too_large", "Files must be 10 MB or smaller", 413)
    digest = hashlib.sha256(data).hexdigest()
    done = db.scalar(select(ImportJob).where(ImportJob.import_type == import_type, ImportJob.file_sha256 == digest,
                                             ImportJob.status == "committed"))
    if done:
        raise AppError("file_already_imported", "This exact file was already imported", 409,
                       {"job_id": done.id, "committed_at": done.committed_at.isoformat() if done.committed_at else None})
    sheets = reader.read(filename, data)  # fail fast on unreadable files, before storing
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    key = storage.save("imports", data, ext[:5])
    job = ImportJob(import_type=import_type, filename=filename[:255], file_sha256=digest, size_bytes=len(data),
                    storage_key=key, created_by=actor_id,
                    sheets=[{"name": s.name, "rows": len(s.rows),
                             "preview": [[normalize.jsonable(c) for c in r] for r in s.rows[:PREVIEW_ROWS]]}
                            for s in sheets])
    db.add(job)
    db.flush()

    # 1) a layout the user mapped before (exact same headers) wins; 2) otherwise auto-detect.
    remembered = _find_remembered(db, import_type, sheets)
    if remembered:
        sheet, header_row, columns = remembered
        job.column_mapping = {"sheet": sheet.name, "header_row": header_row, "columns": columns, "remembered": True}
    else:
        detected = mapping.detect(sheets, import_type)
        if detected:
            job.column_mapping = {"sheet": sheets[detected["sheet_index"]].name, "header_row": detected["header_row"],
                                  "columns": detected["columns"], "remembered": False}
    audit.record(db, action="import.uploaded", entity_type="import_job", entity_id=job.id, actor_id=actor_id,
                 after={"type": import_type, "filename": filename, "sha256": digest}, ctx=ctx)
    _validate(db, job, sheets)
    return job


def _find_remembered(db: Session, import_type: str, sheets: list[reader.Sheet]):
    saved = {t.header_signature: t.mapping for t in db.scalars(
        select(ImportMappingTemplate).where(ImportMappingTemplate.import_type == import_type))}
    if not saved:
        return None
    for sheet in sheets:
        for idx, row in enumerate(sheet.rows[:mapping.HEADER_SCAN_ROWS]):
            if not normalize.is_blank_row(row) and (cols := saved.get(_header_signature(row))):
                return sheet, idx, cols
    return None


def set_mapping(db: Session, *, job: ImportJob, sheet: str, header_row: int, columns: dict[str, int]) -> ImportJob:
    _require_open(job)
    sheets = _load_sheets(job)
    target = next((s for s in sheets if s.name == sheet), None)
    if target is None:
        raise AppError("unknown_sheet", "Sheet not found in the file", 422)
    if not 0 <= header_row < min(len(target.rows), mapping.HEADER_SCAN_ROWS + 50):
        raise AppError("invalid_header_row", "Header row is outside the sheet", 422)
    width = len(target.rows[header_row])
    valid_keys = {f.key for f in mapping.FIELDS[job.import_type]}
    cols = {k: int(v) for k, v in columns.items() if k in valid_keys and v is not None}
    if any(not 0 <= c < width for c in cols.values()) or len(set(cols.values())) != len(cols):
        raise AppError("invalid_mapping", "Each field must map to a different existing column", 422)
    job.column_mapping = {"sheet": sheet, "header_row": header_row, "columns": cols, "remembered": False}
    _validate(db, job, sheets)
    return job


def _require_open(job: ImportJob) -> None:
    if job.status in ("committed", "cancelled"):
        raise AppError("import_closed", f"This import is already {job.status}", 409)


def _rows_for(job: ImportJob, sheets: list[reader.Sheet]) -> tuple[list, list] | None:
    cm = job.column_mapping or {}
    sheet = next((s for s in sheets if s.name == cm.get("sheet")), None)
    if sheet is None:
        return None
    headers = sheet.rows[cm["header_row"]]
    columns = {k: int(v) for k, v in cm.get("columns", {}).items()}
    out = []
    for idx in range(cm["header_row"] + 1, len(sheet.rows)):
        values = sheet.rows[idx]
        if normalize.is_blank_row(values):
            continue
        cells = {k: (values[c] if c < len(values) else None) for k, c in columns.items()}
        raw = {str(headers[i] if i < len(headers) and headers[i] is not None else f"欄{i + 1}"): normalize.jsonable(v)
               for i, v in enumerate(values)}
        out.append((idx + 1, cells, raw))  # 1-based row number as Excel shows it
    return out, headers


def _run(db: Session, job: ImportJob, sheets: list[reader.Sheet]):
    fields = mapping.FIELDS[job.import_type]
    missing = [f.label for f in fields if f.required and f.key not in (job.column_mapping or {}).get("columns", {})]
    if missing:
        return None, missing
    rows, _ = _rows_for(job, sheets) or ([], [])
    return HANDLERS[job.import_type].validate(db, rows, job.id), []


def _validate(db: Session, job: ImportJob, sheets: list[reader.Sheet]) -> None:
    results, missing = _run(db, job, sheets)
    db.execute(delete(ImportRow).where(ImportRow.job_id == job.id))
    if results is None:
        job.status = "parsed"
        job.stats = {"missing_fields": missing}
        db.flush()
        return
    sheet_name = job.column_mapping["sheet"]
    for r in results:
        db.add(ImportRow(job_id=job.id, sheet_name=sheet_name, row_number=r.row_number, raw=r.raw,
                         normalized=r.normalized, matched_sku_id=r.matched_sku_id, proposed_action=r.action,
                         errors=r.errors, warnings=r.warnings))
    job.status = "validated"
    job.stats = _stats(results)
    db.flush()


def _stats(results) -> dict:
    by = {}
    for r in results:
        by[r.action] = by.get(r.action, 0) + 1
    return {"rows": len(results), "by_action": by, "errors": sum(1 for r in results if r.errors),
            "warnings": sum(1 for r in results if r.warnings and not r.errors and r.action != "skip_example"),
            "missing_fields": []}


def commit(db: Session, *, actor_id: int, job: ImportJob, create_missing_skus: bool, skip_error_rows: bool,
           ctx: RequestContext) -> dict:
    _require_open(job)
    if job.status != "validated":
        raise AppError("import_not_ready", "Map the required columns first", 409, job.stats)
    sheets = _load_sheets(job)
    results, missing = _run(db, job, sheets)  # re-check against the database as it is NOW
    if missing:
        raise AppError("import_not_ready", "Map the required columns first", 409, {"missing_fields": missing})
    errors = [r for r in results if r.errors]
    if errors and not skip_error_rows:
        raise AppError("import_has_errors", "Some rows have errors; fix the file or choose to skip those rows", 422,
                       {"error_rows": [r.row_number for r in errors][:50], "count": len(errors)})
    row_ids = dict(db.execute(select(ImportRow.row_number, ImportRow.id).where(ImportRow.job_id == job.id)).all())
    for r in results:
        r.normalized["_import_row_id"] = row_ids.get(r.row_number)
    summary = HANDLERS[job.import_type].commit(db, actor_id=actor_id, job_id=job.id, results=results,
                                               create_missing_skus=create_missing_skus, ctx=ctx)
    summary["skipped_error_rows"] = len(errors)
    job.status, job.committed_by, job.committed_at = "committed", actor_id, datetime.now(UTC)
    job.stats = {**_stats(results), "result": summary}
    _remember_layout(db, job, sheets, actor_id)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise AppError("file_already_imported", "This exact file was already imported", 409) from None
    audit.record(db, action="import.committed", entity_type="import_job", entity_id=job.id, actor_id=actor_id,
                 after={"type": job.import_type, "filename": job.filename, **summary}, ctx=ctx)
    return summary


def _remember_layout(db: Session, job: ImportJob, sheets: list[reader.Sheet], actor_id: int) -> None:
    cm = job.column_mapping
    sheet = next(s for s in sheets if s.name == cm["sheet"])
    sig = _header_signature(sheet.rows[cm["header_row"]])
    tpl = db.scalar(select(ImportMappingTemplate).where(ImportMappingTemplate.import_type == job.import_type,
                                                       ImportMappingTemplate.header_signature == sig))
    if tpl is None:
        db.add(ImportMappingTemplate(name=f"{job.filename} / {sheet.name}", import_type=job.import_type,
                                     header_signature=sig, mapping=cm["columns"], created_by=actor_id))
    else:
        tpl.mapping = cm["columns"]


def cancel(db: Session, *, actor_id: int, job: ImportJob, ctx: RequestContext) -> ImportJob:
    _require_open(job)
    job.status = "cancelled"
    audit.record(db, action="import.cancelled", entity_type="import_job", entity_id=job.id, actor_id=actor_id, ctx=ctx)
    return job
