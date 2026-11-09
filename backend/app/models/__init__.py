"""Import every model module so Base.metadata is complete (used by Alembic)."""
from app.models import (  # noqa: F401
    analytics, auth, catalog, finance, imports, integration, inventory, logistics, purchasing, reference,
    replenishment, sales,
)
