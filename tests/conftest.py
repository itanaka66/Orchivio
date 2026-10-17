from pathlib import Path

import pytest
import yaml

from orchivio.model import AppModel, migrate_to_latest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "inventory-app"


def load_yaml(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


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
