"""Audit sink port (FR-043). Two planes, three channels (DESIGN.md section 9.2)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Plane = Literal["platform", "application"]
Channel = Literal["studio", "api", "mcp"]


@dataclass(frozen=True)
class AuditRecord:
    action: str
    actor_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    plane: Plane = "platform"
    channel: Channel = "api"
    target_type: str | None = None
    target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AuditSink(Protocol):
    def record(self, event: AuditRecord) -> None:
        """Append an event. Must never raise for a well-formed record."""
        ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditRecord] = []

    def record(self, event: AuditRecord) -> None:
        self.events.append(event)
