from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def liveness() -> dict:
    """Process is up (no dependencies checked)."""
    return {"status": "ok"}


@router.get("/readyz")
def readiness(db: Session = Depends(get_db)):
    """Ready to serve: database reachable and migrated to the expected head."""
    try:
        version = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "unreachable"})
    if version is None:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "not migrated"})
    return {"status": "ok", "database": "ok", "schema_version": version}
