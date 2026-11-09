"""Test fixtures. Tests run against a REAL PostgreSQL database (ERP_TEST_DATABASE_URL),
migrated from scratch with Alembic, so triggers and constraints are exercised exactly as in
production. Each test runs inside a transaction that is rolled back afterwards."""
import os
import uuid
from datetime import UTC, datetime

import pytest

TEST_DB_URL = os.environ.get(
    "ERP_TEST_DATABASE_URL", "postgresql+psycopg://erp:erp_dev_pw@localhost:5432/erp_test"
)
os.environ["ERP_DATABASE_URL"] = TEST_DB_URL
os.environ["ERP_ENV"] = "test"
os.environ["ERP_LOG_JSON"] = "false"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def alembic_config() -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    cfg.attributes["database_url"] = TEST_DB_URL
    return cfg


@pytest.fixture(scope="session")
def engine():
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = create_engine(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Session inside an outer transaction; rolled back after the test.
    Use `with db.begin_nested():` around statements that are expected to fail."""
    conn = engine.connect()
    trans = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        conn.close()


# ---------- small builders (test data only; never used outside tests) ----------

def uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def loc(db: Session, code: str) -> int:
    return db.execute(text("SELECT id FROM locations WHERE code = :c"), {"c": code}).scalar_one()


def make_sku(db: Session, *, is_bundle: bool = False, code: str | None = None) -> int:
    pid = db.execute(text("INSERT INTO products (name) VALUES ('test product') RETURNING id")).scalar_one()
    return db.execute(
        text("INSERT INTO skus (product_id, sku_code, name, is_bundle) VALUES (:p, :c, 'test sku', :b) RETURNING id"),
        {"p": pid, "c": code or uid("T-"), "b": is_bundle},
    ).scalar_one()


def make_lot(db: Session, sku_id: int, qty: int = 10, unit_cost: str = "5.000000") -> int:
    return db.execute(
        text("""INSERT INTO inventory_lots (sku_id, lot_no, source, received_at, original_qty, original_unit_cost_base)
                VALUES (:s, :n, 'opening_balance', :t, :q, :c) RETURNING id"""),
        {"s": sku_id, "n": uid("LOT-"), "t": datetime.now(UTC), "q": qty, "c": unit_cost},
    ).scalar_one()


def move(db: Session, sku_id: int, lot_id: int, mtype: str, frm: str, to: str, qty: int, key: str | None = None) -> int:
    return db.execute(
        text("""INSERT INTO inventory_movements
                  (sku_id, lot_id, from_location_id, to_location_id, qty, movement_type, idempotency_key)
                VALUES (:s, :l, :f, :t, :q, :m, :k) RETURNING id"""),
        {"s": sku_id, "l": lot_id, "f": loc(db, frm), "t": loc(db, to), "q": qty, "m": mtype,
         "k": key or uid("mv-")},
    ).scalar_one()


def position(db: Session, sku_id: int) -> dict:
    row = db.execute(text("SELECT * FROM v_inventory_position WHERE sku_id = :s"), {"s": sku_id}).mappings().one()
    return dict(row)
