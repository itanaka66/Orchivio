"""Persist application log lines (FR-123) so builders can read them in Studio.

Runtime code (jobs, webhook delivery, integrations) logs to ``orchivio.app`` with ``extra={"fields": {...}}``; this handler stores
the lines that name an application. Content is restricted twice: call sites pass fixed messages, and only allow-listed scalar
fields are kept here. Writes are best effort in their own transaction, like the audit sink, so logging never breaks a request.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from orchivio import telemetry
from orchivio.db.models import AppLog

LOGGER = telemetry.APP_LOGGER
ALLOWED_FIELDS = frozenset({"event", "flow", "process", "trigger", "outcome", "status", "status_class", "attempt", "attempts",
                            "integration", "destination", "operation", "webhook", "event_type", "environment", "duration_ms"})
_LEVEL = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error"}
PRUNE_EVERY = 200


class AppLogHandler(logging.Handler):
    def __init__(self, session_factory: sessionmaker[Session], retention_days: int) -> None:
        super().__init__(level=logging.INFO)
        self._sf = session_factory
        self.retention = timedelta(days=max(1, retention_days))
        self._writes = 0

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "fields", None)
        if not isinstance(fields, dict) or "application_id" not in fields:
            return
        try:
            app_id = uuid.UUID(str(fields["application_id"]))
            keep = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS and isinstance(v, (str, int, float, bool))}
            ids = telemetry.current_ids()
            rid = telemetry.request_id_var.get()
            with self._sf() as s:
                s.add(AppLog(application_id=app_id, level=_LEVEL.get(record.levelno, "info"), source=str(fields.get("source", "system"))[:24],
                             message=record.getMessage()[:300], request_id=None if rid == "-" else rid[:64],
                             trace_id=ids[0] if ids else None, fields=keep))
                self._writes += 1
                if self._writes % PRUNE_EVERY == 0:
                    s.execute(delete(AppLog).where(AppLog.created_at < datetime.now(UTC) - self.retention))
                s.commit()
        except Exception:  # noqa: BLE001 - logging must never break the caller
            pass


def install(session_factory: sessionmaker[Session], retention_days: int) -> AppLogHandler:
    """Attach the handler, replacing an earlier one (a process runs one platform database)."""
    lg = logging.getLogger(LOGGER)
    for h in list(lg.handlers):
        if isinstance(h, AppLogHandler):
            lg.removeHandler(h)
    handler = AppLogHandler(session_factory, retention_days)
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    return handler
