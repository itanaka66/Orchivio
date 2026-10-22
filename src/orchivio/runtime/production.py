"""Production runtime store (FR-030, FR-055, FR-094, FR-097).

A ``ProductionStore`` manages the runtime tables for a *deployed* environment.
Unlike ``PreviewStore``, production data is persistent across resets; ``build()``
creates or migrates tables but never truncates them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    inspect,
    select,
)

from orchivio.model import AppModel
from orchivio.ports import AuditSink, IntegrationClient

from . import outbox
from .service import AppData, Hooks
from .tables import RuntimeSchema, table_prefix

_prod_meta = MetaData()
PROD_META = Table(
    "orchivio_prod_meta", _prod_meta,
    Column("prefix", String(64), primary_key=True),
    Column("model_hash", String(80), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
    Column("last_migrated_at", DateTime(timezone=True), nullable=True),
)

# Cached engines keyed by data_url to avoid opening a new connection pool per request.
_engine_cache: dict[str, Engine] = {}


def _get_engine(data_url: str) -> Engine:
    if data_url not in _engine_cache:
        _engine_cache[data_url] = create_engine(data_url, pool_pre_ping=True)
    return _engine_cache[data_url]


class ProductionStore:
    """Manages production runtime data for one application in an environment with ``data_url``."""

    def __init__(self, data_url: str) -> None:
        self.engine: Engine = _get_engine(data_url)
        _prod_meta.create_all(self.engine)
        outbox.ensure(self.engine)

    def status(self, application_id: uuid.UUID) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(PROD_META).where(PROD_META.c.prefix == table_prefix(application_id))).mappings().first()
        if row is None:
            return None
        return {"model_hash": row["model_hash"], "built_at": row["built_at"], "last_migrated_at": row.get("last_migrated_at")}

    def build(self, application_id: uuid.UUID, model: AppModel, model_hash: str, *,
              audit: AuditSink | None = None, organization_id: uuid.UUID | None = None,
              hooks: Hooks | None = None, integrations: IntegrationClient | None = None,
              mcp_broker: object | None = None) -> tuple[RuntimeSchema, AppData]:
        """Create (or expand) production tables from ``model`` without dropping existing data."""
        schema = RuntimeSchema(model, table_prefix(application_id))
        schema.create_all(self.engine)  # CREATE TABLE IF NOT EXISTS — safe to re-run
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            existing = conn.execute(select(PROD_META).where(PROD_META.c.prefix == table_prefix(application_id))).mappings().first()
            if existing is None:
                conn.execute(insert(PROD_META).values(prefix=table_prefix(application_id), model_hash=model_hash, built_at=now))
            else:
                from sqlalchemy import update
                conn.execute(update(PROD_META).where(PROD_META.c.prefix == table_prefix(application_id))
                             .values(model_hash=model_hash, last_migrated_at=now))
        return schema, AppData(self.engine, schema, audit=audit, application_id=application_id,
                               organization_id=organization_id, hooks=hooks, integrations=integrations,
                               mcp_broker=mcp_broker)

    def open(self, application_id: uuid.UUID, model: AppModel, model_hash: str, *,
             audit: AuditSink | None = None, organization_id: uuid.UUID | None = None,
             hooks: Hooks | None = None, integrations: IntegrationClient | None = None,
             mcp_broker: object | None = None) -> AppData:
        schema = RuntimeSchema(model, table_prefix(application_id))
        return AppData(self.engine, schema, audit=audit, application_id=application_id,
                       organization_id=organization_id, hooks=hooks, integrations=integrations,
                       mcp_broker=mcp_broker)
