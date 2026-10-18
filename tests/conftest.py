from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from orchivio.model import AppModel, migrate_to_latest
from tests_support import load_yaml


@event.listens_for(Engine, "connect")
def _fast_sqlite(dbapi_conn, _record):
    """Durability is irrelevant for throwaway test databases and fsync dominates the run time."""
    if dbapi_conn.__class__.__module__.startswith("sqlite3"):
        dbapi_conn.execute("PRAGMA synchronous=OFF")
        dbapi_conn.execute("PRAGMA journal_mode=MEMORY")


@pytest.fixture
def raw_model() -> dict:
    return load_yaml("model.yaml")


@pytest.fixture
def model(raw_model) -> AppModel:
    return AppModel.model_validate(migrate_to_latest(raw_model))


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory) -> Path:
    """A SQLite file migrated to head once; tests get cheap copies of it."""
    from alembic import command
    from sqlalchemy import create_engine

    from tests_support import alembic_config

    path = tmp_path_factory.mktemp("template") / "template.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    engine.dispose()
    return path


@pytest.fixture
def db_url(migrated_template, tmp_path) -> str:
    import shutil

    dest = tmp_path / "test.db"
    shutil.copy(migrated_template, dest)
    return f"sqlite:///{dest}"


@pytest.fixture
def app(db_url):
    from sqlalchemy import event

    from orchivio.api import create_app
    from orchivio.api.settings import Settings
    from orchivio.db import make_engine

    engine = make_engine(db_url)

    @event.listens_for(engine, "connect")
    def _fast(dbapi_conn, _):  # durability is irrelevant for throwaway test databases
        dbapi_conn.execute("PRAGMA synchronous=OFF")

    preview_url = db_url.replace("test.db", "preview.db")
    artifacts = db_url.replace("sqlite:///", "").replace("test.db", "artifacts")
    return create_app(Settings(env="dev", database_url=db_url, preview_database_url=preview_url,
                               artifact_dir=artifacts), engine=engine,
                      preview_engine=make_engine(preview_url))


@pytest.fixture
def new_api(app):
    """Factory for independent logged-in clients (separate cookie jars) on one app."""
    from tests_support import Api

    counter = iter(range(1000))

    def make(email: str | None = None) -> "Api":
        return Api(app).signup(email or f"user{next(counter)}@example.com")

    return make
