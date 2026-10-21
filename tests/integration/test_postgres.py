"""PostgreSQL-only behaviour. Skipped unless ORCHIVIO_TEST_PG_URL points at a *disposable* database
(for example postgresql+psycopg://orchivio:orchivio@localhost:5432/orchivio). CI provides one.

These cover what SQLite cannot: immutability triggers, the circular foreign keys, JSONB/UUID[]
types, the PostgreSQL dialect branches of the SQL predicate compiler, and the runtime on real types.
"""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from orchivio.db import RevisionRepository
from orchivio.db.models import Application, AuditEvent, Organization, Project, User
from orchivio.expr import CheckEnv, SqlContext, UserRef, check, compile_predicate
from orchivio.model import AppModel, assign_element_ids, migrate_to_latest, model_hash
from orchivio.testgen import run_tests
from tests_support import alembic_config, load_yaml

URL = os.environ.get("ORCHIVIO_TEST_PG_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="set ORCHIVIO_TEST_PG_URL to a disposable PostgreSQL database"),
]

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def engine():
    eng = create_engine(URL)
    yield eng
    eng.dispose()


def _load_cases():
    spec = importlib.util.spec_from_file_location("expr_sql_cases", ROOT / "tests" / "unit" / "test_expr_sql.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sql_predicates_match_the_interpreter_on_postgres(engine):
    cases = _load_cases()
    failures = []
    with engine.connect() as conn:
        conn.execute(text("CREATE TEMP TABLE t (id int, owner text, qty int, label text, name text, "
                          "archived boolean, status text, manager text)"))
        conn.execute(text("INSERT INTO t VALUES (:id,:owner,:qty,:label,:name,:archived,:status,:manager)"), cases.ROWS)
        for user in (UserRef("u1", {"editor"}), UserRef("u2")):
            for src in cases.EXPRS:
                res = check(src, CheckEnv(cases.Schema(), cases.ENTITY))
                pred = compile_predicate(res, SqlContext(current_user=user, dialect="postgresql"))
                try:
                    got = [r[0] for r in conn.execute(text(f"SELECT id FROM t WHERE {pred.sql} ORDER BY id"), pred.params)]
                except DBAPIError as e:
                    conn.rollback()
                    conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS t (id int)"))
                    failures.append((src, f"SQL error: {str(e.orig)[:120]}"))
                    continue
                want = cases._filter_interp(src, user)
                if got != want:
                    failures.append((src, f"sql={got} interpreter={want}"))
    assert failures == []


def test_migrations_triggers_and_foreign_keys_on_postgres(engine):
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    try:
        with Session(engine) as s:
            user, org = User(email="pg@example.com", display_name="PG"), Organization(name="O", slug="pg-org")
            s.add_all([user, org])
            s.flush()
            project = Project(organization_id=org.id, name="P", slug="pg-proj")
            s.add(project)
            s.flush()
            app = Application(project_id=project.id, key="inv", name="Inv")
            s.add(app)
            s.flush()
            model = AppModel.model_validate(migrate_to_latest(load_yaml("model.yaml")))
            rev = RevisionRepository(s).create(app.id, model, created_by=user.id, organization_id=org.id)
            s.add(AuditEvent(action="pg.test"))
            s.commit()
            app_id, rev_id = app.id, rev.id
            assert rev.parent_revision_ids == [] and isinstance(rev.model_json, dict)

        def fails(sql, **params):
            with pytest.raises(DBAPIError), engine.begin() as conn:
                conn.execute(text(sql), params)

        fails("UPDATE app_revisions SET message = 'tampered' WHERE id = :i", i=rev_id)  # immutability trigger
        fails("UPDATE audit_events SET action = 'tampered'")  # append-only trigger
        fails("DELETE FROM audit_events")
        fails("UPDATE applications SET current_revision_id = :r WHERE id = :a", r=uuid.uuid4(), a=app_id)  # circular FK
        with engine.begin() as conn:  # legitimate changes still work
            conn.execute(text("UPDATE applications SET name = 'Renamed' WHERE id = :a"), {"a": app_id})
    finally:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")


def test_runtime_and_generated_tests_on_postgres(engine):
    model = assign_element_ids(AppModel.model_validate(migrate_to_latest(load_yaml("model.yaml"))))
    report = run_tests(model, model_hash(model), engine, seed=2)
    failed = [(r.id, r.message) for r in report.results if r.status == "failed"]
    assert failed == []
    assert report.counts["skipped"] == 0 and report.counts["passed"] >= 20


def test_backup_and_restore_on_postgres(engine):
    """Circular foreign keys, the identity sequence and timestamptz must survive a restore."""
    from datetime import UTC, datetime

    from orchivio.ops import backup, restore

    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    try:
        with Session(engine) as s:
            user, org = User(email="bk@example.com", display_name="BK"), Organization(name="O", slug="bk-org")
            s.add_all([user, org])
            s.flush()
            project = Project(organization_id=org.id, name="P", slug="bk-proj")
            s.add(project)
            s.flush()
            app = Application(project_id=project.id, key="inv", name="Inv")
            s.add(app)
            s.flush()
            model = AppModel.model_validate(migrate_to_latest(load_yaml("model.yaml")))
            RevisionRepository(s).create(app.id, model, created_by=user.id, organization_id=org.id)
            s.add_all([AuditEvent(action="bk.one"), AuditEvent(action="bk.two")])
            s.commit()
        data = backup(engine, now=datetime(2026, 1, 1, tzinfo=UTC))

        with engine.begin() as conn:  # an empty database at the same revision
            command.downgrade(alembic_config(conn), "base")
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        report = restore(data, engine)
        assert report.tables["applications"] == 1 and report.tables["audit_events"] >= 2

        with engine.connect() as conn:
            current = conn.execute(text("SELECT current_revision_id FROM applications")).scalar()
            assert current is not None  # the deferred circular reference was filled in
            assert conn.execute(text("SELECT created_at FROM users")).scalar().tzinfo is not None
        with Session(engine) as s:  # the identity sequence continues after the restored ids
            existing = s.scalar(text("SELECT MAX(id) FROM audit_events"))
            s.add(AuditEvent(action="bk.after"))
            s.commit()
            assert s.scalar(text("SELECT MAX(id) FROM audit_events")) > existing
    finally:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")
