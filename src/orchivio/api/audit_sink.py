"""Database-backed audit sink for application-plane events (own transaction per event)."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session, sessionmaker

from orchivio.db.models import AuditEvent
from orchivio.ports import AuditRecord

log = logging.getLogger("orchivio.audit")


class DbAuditSink:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def record(self, event: AuditRecord) -> None:
        try:
            with self._sf() as s:
                s.add(AuditEvent(organization_id=event.organization_id, actor_id=event.actor_id, plane=event.plane,
                                 channel=event.channel, action=event.action, target_type=event.target_type,
                                 target_id=event.target_id, meta=event.metadata))
                s.commit()
        except Exception:  # noqa: BLE001 - auditing must not turn a completed write into a 500
            log.exception("failed to write audit event %s", event.action)
