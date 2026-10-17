"""Port for calling consumed REST APIs from flows (FR-081, docs/INTEGRATIONS.md).

The Logic Engine builds an ``IntegrationCall`` from the model's typed operation and the evaluated arguments; an
adapter performs it. The real adapter resolves the logical ``destination`` to a configured, allowlisted base URL
(``orchivio.integrations.rest``); flow tests use ``MockIntegrations`` and never reach a network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class IntegrationError(Exception):
    """The call could not be made or the destination refused it. The message must be safe to show to flow authors."""


@dataclass(frozen=True)
class IntegrationCall:
    integration: str  # key within its module
    operation: str
    destination: str  # logical name, resolved by the adapter
    method: str
    path: str  # placeholders already filled with percent-encoded values
    query: Mapping[str, str] = field(default_factory=dict)
    body: Any = None
    timeout_seconds: int = 10


@dataclass(frozen=True)
class IntegrationResult:
    status: int
    body: Any = None  # parsed JSON, or None when the answer had no JSON body


class IntegrationClient(Protocol):
    def call(self, call: IntegrationCall) -> IntegrationResult: ...


class MockIntegrations:
    """Canned answers keyed ``"<integration>.<operation>"``; anything else is an error, so tests fail loudly."""

    def __init__(self, mocks: Mapping[str, Any] | None = None) -> None:
        self.mocks = dict(mocks or {})
        self.calls: list[IntegrationCall] = []

    def call(self, call: IntegrationCall) -> IntegrationResult:
        self.calls.append(call)
        mock = self.mocks.get(f"{call.integration}.{call.operation}")
        if mock is None:
            raise IntegrationError(f"no mock for {call.integration}.{call.operation} in this test")
        return IntegrationResult(mock.status, mock.json_body)
