"""Helpers shared by test modules."""

from pathlib import Path

from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg
