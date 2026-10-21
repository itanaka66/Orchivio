"""Entity event hooks backed by flows (FR-054). Implements ``orchivio.runtime.Hooks``."""

from __future__ import annotations

from typing import Any

from orchivio.model import AppModel
from orchivio.runtime import AppData, AppUser

from .engine import DEFAULT_LIMITS, FlowRunner, Limits


class FlowHooks:
    """Pass an instance as ``AppData(hooks=...)``; the entity's ``hooks`` map names the flows."""

    def __init__(self, model: AppModel, limits: Limits = DEFAULT_LIMITS,
                 constants: dict[str, Any] | None = None) -> None:
        self.model, self.limits = model, limits
        self.constants: dict[str, Any] = constants or {}

    def run(self, data: AppData, flow: str, event: str, entity: str, user: AppUser | None,
            record: dict[str, Any], previous: dict[str, Any] | None) -> None:
        # The hook runs inside the operation's transaction and as the same user: a failing hook rolls
        # back the write that triggered it, and hooks cannot do more than the caller may.
        module_key = entity.split(".", 1)[0]
        FlowRunner(self.model, data, limits=self.limits, constants=self.constants or None).run_hook(
            f"{module_key}.{flow}" if "." not in flow else flow, user, record, previous, entity)
