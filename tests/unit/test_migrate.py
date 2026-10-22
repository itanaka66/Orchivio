"""Unit tests for expand/contract data migrations (FR-034, FR-097)."""

import pytest
from sqlalchemy import create_engine, text

from orchivio.delivery.migrate import run_expand, run_contract


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    yield eng
    eng.dispose()


def _create_product(conn, prefix="app_"):
    conn.execute(text(f'CREATE TABLE "{prefix}core__Product" (id TEXT PRIMARY KEY, sku TEXT)'))
    conn.execute(text(f"INSERT INTO \"{prefix}core__Product\" VALUES ('r1', 'ABC-1')"))
    conn.execute(text(f"INSERT INTO \"{prefix}core__Product\" VALUES ('r2', 'DEF-2')"))


def test_run_expand_copies_data_for_renamed_field(engine):
    """When a field is renamed, copy_from should populate the new column from the old one."""
    prefix = "app_"
    with engine.begin() as conn:
        _create_product(conn, prefix)
        # Pre-add the new column so we can test the copy without PostgreSQL-specific DDL
        conn.execute(text(f'ALTER TABLE "{prefix}core__Product" ADD COLUMN code TEXT'))

    step = {
        "op": "add_column",
        "phase": "expand",
        "target": "core.Product.code",
        "type": "string",
        "required": False,
        "copy_from": "core.Product.sku",
    }
    with engine.begin() as conn:
        applied = run_expand(conn, [step], prefix)

    assert any("copy_data" in s for s in applied)

    with engine.connect() as conn:
        rows = conn.execute(text(f'SELECT sku, code FROM "{prefix}core__Product" ORDER BY id')).mappings().all()

    assert [r["code"] for r in rows] == ["ABC-1", "DEF-2"]


def test_run_expand_copy_is_idempotent(engine):
    """Running the copy step twice should not overwrite already-filled values."""
    prefix = "app_"
    with engine.begin() as conn:
        _create_product(conn, prefix)
        conn.execute(text(f'ALTER TABLE "{prefix}core__Product" ADD COLUMN code TEXT'))
        conn.execute(text(f"UPDATE \"{prefix}core__Product\" SET code = 'MANUAL' WHERE id = 'r1'"))

    step = {
        "op": "add_column",
        "phase": "expand",
        "target": "core.Product.code",
        "type": "string",
        "required": False,
        "copy_from": "core.Product.sku",
    }
    with engine.begin() as conn:
        run_expand(conn, [step], prefix)

    with engine.connect() as conn:
        rows = conn.execute(text(f'SELECT id, code FROM "{prefix}core__Product" ORDER BY id')).mappings().all()

    # r1 was already filled with 'MANUAL' — copy must not overwrite
    assert rows[0]["code"] == "MANUAL"
    assert rows[1]["code"] == "DEF-2"


def test_run_expand_warns_backfill_required(engine):
    """A new required column without a default should emit a backfill warning."""
    prefix = "app_"
    with engine.begin() as conn:
        _create_product(conn, prefix)
        conn.execute(text(f'ALTER TABLE "{prefix}core__Product" ADD COLUMN score TEXT'))

    step = {
        "op": "add_column",
        "phase": "expand",
        "target": "core.Product.score",
        "type": "string",
        "required": False,
        "backfill_required": True,
    }
    with engine.begin() as conn:
        applied = run_expand(conn, [step], prefix)

    assert any("WARNING" in s and "backfill_required" in s for s in applied)


def test_run_expand_no_copy_without_copy_from(engine):
    """A plain add_column without copy_from should not emit a copy_data line."""
    prefix = "app_"
    with engine.begin() as conn:
        _create_product(conn, prefix)
        conn.execute(text(f'ALTER TABLE "{prefix}core__Product" ADD COLUMN note TEXT'))

    step = {
        "op": "add_column",
        "phase": "expand",
        "target": "core.Product.note",
        "type": "text",
        "required": False,
    }
    with engine.begin() as conn:
        applied = run_expand(conn, [step], prefix)

    assert not any("copy_data" in s for s in applied)
    assert not any("WARNING" in s for s in applied)
