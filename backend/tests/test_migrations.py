from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.conftest import TEST_DB_URL, alembic_config


def test_models_and_migrations_are_in_sync(engine):
    # Raises if the ORM models contain anything the migrations don't create (or vice versa).
    command.check(alembic_config())


def test_full_downgrade_then_upgrade_roundtrip(engine):
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    eng = create_engine(TEST_DB_URL)
    with eng.connect() as c:
        remaining = [t for t in inspect(c).get_table_names() if t != "alembic_version"]
    assert remaining == []
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0004"
        assert len(inspect(c).get_table_names()) == 58  # 57 business tables + alembic_version
    eng.dispose()
