"""Preview environment lifecycle (FR-035: preview data is isolated from production data).

The preview database is separate from the platform database (``preview_database_url``). It is
rebuilt from the model on every reset; there is no data migration between model versions
(production-grade migrations are planned, FR-034). The meta table records which model hash the
tables were built from, so a stale preview is detected instead of used.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime, Engine, MetaData, String, Table, delete, inspect, insert, select

from orchivio.model import AppModel
from orchivio.ports import AuditSink

from .service import AppData, Hooks
from .tables import RuntimeSchema, table_prefix

_meta = MetaData()
META = Table(
    "orchivio_preview_meta", _meta,
    Column("prefix", String(64), primary_key=True),
    Column("model_hash", String(80), nullable=False),
    Column("built_at", DateTime(timezone=True), nullable=False),
)


class PreviewNotBuilt(Exception):
    pass


class PreviewStale(Exception):
    pass


class PreviewStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        _meta.create_all(engine)

    def status(self, application_id: uuid.UUID) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(META).where(META.c.prefix == table_prefix(application_id))).mappings().first()
        return None if row is None else {"model_hash": row["model_hash"], "built_at": row["built_at"]}

    def drop(self, application_id: uuid.UUID) -> None:
        prefix = table_prefix(application_id)
        names = [n for n in inspect(self.engine).get_table_names() if n.startswith(prefix)]
        if names:
            md = MetaData()
            md.reflect(self.engine, only=names)
            md.drop_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(delete(META).where(META.c.prefix == prefix))

    def reset(self, application_id: uuid.UUID, model: AppModel, model_hash: str, *,
              audit: AuditSink | None = None, organization_id: uuid.UUID | None = None,
              hooks: Hooks | None = None) -> tuple[RuntimeSchema, AppData]:
        """Drop this application's preview tables and rebuild them (empty) from ``model``."""
        self.drop(application_id)
        schema = RuntimeSchema(model, table_prefix(application_id))
        schema.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(META).values(prefix=table_prefix(application_id), model_hash=model_hash,
                                             built_at=datetime.now(UTC)))
        return schema, AppData(self.engine, schema, audit=audit, application_id=application_id,
                               organization_id=organization_id, hooks=hooks)

    def open(self, application_id: uuid.UUID, model: AppModel, model_hash: str, *,
             audit: AuditSink | None = None, organization_id: uuid.UUID | None = None,
             hooks: Hooks | None = None) -> AppData:
        status = self.status(application_id)
        if status is None:
            raise PreviewNotBuilt("the preview has not been built")
        if status["model_hash"] != model_hash:
            raise PreviewStale("the model changed since the preview was built; reset it")
        schema = RuntimeSchema(model, table_prefix(application_id))
        return AppData(self.engine, schema, audit=audit, application_id=application_id, organization_id=organization_id,
                       hooks=hooks)
