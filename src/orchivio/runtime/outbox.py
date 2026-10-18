"""Transactional outbox for business events (FR-083).

Events are inserted into ``orchivio_outbox`` *in the same database transaction as the data change* that caused
them (the application's runtime database), so a rolled-back write or flow produces no event and a committed one
can never lose its event. A relay later copies rows to the platform database, which is a different database;
that hop is at-least-once and made idempotent by a unique ``(subscription, event)`` key there.
"""

from __future__ import annotations

import threading
import uuid
import weakref
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Engine,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    select,
    update,
)

_meta = MetaData()
OUTBOX = Table(
    "orchivio_outbox", _meta,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("event_id", String(36), nullable=False, unique=True),
    Column("application_id", String(36), nullable=False),
    Column("type", String(200), nullable=False),
    Column("subject", String(200), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("relayed_at", DateTime(timezone=True), nullable=True),
    Index("ix_orchivio_outbox_pending", "relayed_at", "id"),
)

_ensured: weakref.WeakSet[Engine] = weakref.WeakSet()
_lock = threading.Lock()


def ensure(engine: Engine) -> None:
    """Create the outbox table once per engine."""
    if engine in _ensured:
        return
    with _lock:
        _meta.create_all(engine)
        _ensured.add(engine)


def add(conn: Any, application_id: uuid.UUID, type_: str, subject: str, payload: dict[str, Any],
        occurred_at: datetime | None = None) -> str:
    """Insert an event on ``conn`` (the caller's open transaction). Returns the event id."""
    event_id = str(uuid.uuid4())
    conn.execute(OUTBOX.insert().values(event_id=event_id, application_id=str(application_id), type=type_, subject=subject,
                                        occurred_at=occurred_at or datetime.now(UTC), payload=payload))
    return event_id


def pending(engine: Engine, limit: int = 200) -> list[dict[str, Any]]:
    ensure(engine)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(OUTBOX).where(OUTBOX.c.relayed_at.is_(None)).order_by(OUTBOX.c.id).limit(limit)).mappings()]


def mark_relayed(engine: Engine, ids: list[int]) -> None:
    if ids:
        with engine.begin() as conn:
            conn.execute(update(OUTBOX).where(OUTBOX.c.id.in_(ids)).values(relayed_at=datetime.now(UTC)))


def purge(engine: Engine, *, application_id: uuid.UUID | None = None, relayed_before: datetime | None = None) -> None:
    ensure(engine)
    with engine.begin() as conn:
        if application_id is not None:
            conn.execute(delete(OUTBOX).where(OUTBOX.c.application_id == str(application_id)))
        if relayed_before is not None:
            conn.execute(delete(OUTBOX).where(OUTBOX.c.relayed_at.is_not(None), OUTBOX.c.relayed_at < relayed_before))
