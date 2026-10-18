"""Provider-neutral LLM interface (FR-018, DESIGN.md sections 6.3 and 7).

Hosted and local providers implement ``LLMProvider``. Callers never import a vendor SDK.
Model output is always untrusted: use ``generate_validated`` to get schema-checked data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost_usd + other.cost_usd,
        )


NO_USAGE = Usage()  # a shared, immutable zero (usable as a default argument)


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[Message, ...]
    model: str | None = None
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout_s: float = 60.0
    # JSON Schema the output must satisfy (providers may use native structured output).
    response_schema: dict[str, Any] | None = None
    prompt_template: str | None = None  # name@version, recorded for audit (section 7.4)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    usage: Usage = Usage()
    latency_ms: int = 0
    finish_reason: str = "stop"


class LLMError(Exception):
    """Provider failure (network, quota, refusal). Not retried by ``generate_validated``."""


class LLMTimeout(LLMError):
    pass


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    # True if prompts leave the deployment (hosted API). Callers must have consent (A3,
    # DESIGN.md section 12.3) before sending project data to such a provider.
    external: bool

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return one completion. Must honour ``request.timeout_s`` and raise ``LLMTimeout``."""
        ...
