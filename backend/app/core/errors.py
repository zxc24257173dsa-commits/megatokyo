"""Uniform error envelope: {"error": {"code", "message", "details", "request_id"}}.

Database integrity errors are translated into 409 responses with a stable code
so the frontend can show a meaningful message (e.g. duplicate SKU) instead of a 500.
Internal details (SQL, stack traces) are logged, never returned to the client.
"""
import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

log = logging.getLogger("app.errors")


class AppError(Exception):
    """Business-rule violation raised by services."""

    def __init__(self, code: str, message: str, http_status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}


def _body(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}, "request_id": request_id_var.get()}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.http_status, content=_body(exc.code, exc.message, exc.details))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content=_body(f"http_{exc.status_code}", str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_body("validation_error", "Request validation failed", {"errors": exc.errors()}),
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError):
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        log.warning("integrity error", extra={"extra_fields": {"constraint": constraint}})
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_body("conflict", "Data conflicts with an existing record or rule", {"constraint": constraint}),
        )

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError):
        # Raised by our protective triggers (append-only ledgers, closed periods, locked batches).
        msg = str(getattr(exc.orig, "diag", None) and exc.orig.diag.message_primary or "")
        if msg.startswith("ERP_"):
            code, _, text = msg.partition(":")
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=_body(code.lower(), text.strip()))
        log.exception("database error")
        return JSONResponse(status_code=500, content=_body("database_error", "Internal database error"))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content=_body("internal_error", "Internal server error"))
