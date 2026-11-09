import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import auth, catalog, health, imports, inventory, users, whatnot
from app.core.config import get_settings
from app.core.errors import AppError, register_exception_handlers
from app.core.logging import configure_logging, request_id_var

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
log = logging.getLogger("app.request")

app = FastAPI(title=settings.app_name, version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
    response.headers["X-Request-ID"] = rid
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/v1/auth"):
        response.headers.setdefault("Cache-Control", "no-store")
    log.info(
        "request",
        extra={"extra_fields": {
            "method": request.method, "path": request.url.path,
            "status": response.status_code, "duration_ms": duration_ms,
        }},
    )
    request_id_var.reset(token)
    return response


register_exception_handlers(app)
app.include_router(health.router)
app.include_router(health.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(catalog.router, prefix="/api/v1")
app.include_router(inventory.router, prefix="/api/v1")
app.include_router(imports.router, prefix="/api/v1")
app.include_router(whatnot.router, prefix="/api/v1")


# ---------- single-page app (production) ----------
_dist = Path(settings.frontend_dist).resolve()
if (_dist / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise AppError("http_404", "Not Found", 404)
        file = (_dist / path).resolve()
        if path and _dist in file.parents and file.is_file():
            return FileResponse(file)
        return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
