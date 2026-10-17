"""The Alembic migrations must produce exactly the schema the ORM models describe."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from orchivio.db.models import Base
from tests_support import alembic_config

# SQLite cannot add constraints after table creation, so the two circular FKs are
# PostgreSQL-only (see the migration).
_PG_ONLY_FKS = {"fk_applications_current_revision", "fk_branches_head_revision"}


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    yield eng
    eng.dispose()


def test_upgrade_matches_models(engine):
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    unexpected = [
        d
        for d in diffs
        if not (d[0] == "add_fk" and d[1].name in _PG_ONLY_FKS)
    ]
    assert unexpected == []


def test_all_model_tables_exist(engine):
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)


def test_downgrade_removes_everything(engine):
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set()


def test_email_uniqueness_is_case_insensitive(engine):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    ins = text(
        "INSERT INTO users (id, email, display_name, status) VALUES (:i, :e, 'n', 'active')"
    )
    with engine.begin() as conn:
        conn.execute(ins, {"i": "00000000-0000-0000-0000-000000000001", "e": "A@Example.com"})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(ins, {"i": "00000000-0000-0000-0000-000000000002", "e": "a@example.COM"})
