"""Processes and the task inbox (FR-110, FR-111)."""

from .engine import Hooks, Machine, ProcessError, State, TaskSpec, Token, resolve_definition
from .service import ProcessService, ServiceError


def ticker(sessions, preview, *, audit=None, integrations=None):
    """A no-argument callable for the background worker: fires due timers and escalates overdue tasks."""

    def run() -> dict[str, int]:
        with sessions() as s:
            return ProcessService(s, preview, audit=audit, integrations=integrations).tick()

    return run


__all__ = ["ProcessService", "ServiceError", "ticker", "Hooks", "Machine", "ProcessError", "State", "TaskSpec", "Token", "resolve_definition"]
