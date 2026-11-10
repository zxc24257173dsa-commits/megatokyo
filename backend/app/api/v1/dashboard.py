"""/api/v1/dashboard (Phase 11): the home page. Each block depends on the user's permissions."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_active_user
from app.db.session import get_db
from app.services import dashboard

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def get_dashboard(current: CurrentUser = Depends(get_active_user), db: Session = Depends(get_db)):
    return dashboard.build(db, set(current.permissions))
