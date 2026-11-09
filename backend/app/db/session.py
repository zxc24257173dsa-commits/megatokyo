from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)


@event.listens_for(engine, "connect")
def _on_connect(dbapi_conn, _):
    # All timestamps are stored and compared in UTC; presentation converts per user.
    with dbapi_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute(f"SET statement_timeout = {int(_settings.db_statement_timeout_ms)}")
    dbapi_conn.commit()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request; rollback on error."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
