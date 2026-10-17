"""Deterministic provider for tests, demos and offline development."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from .base import LLMError, LLMRequest, LLMResponse, Usage

Responder = Callable[[LLMRequest], str]


class MockProvider:
    """Replays scripted responses in order, or delegates to a ``Responder`` callable.

    Scripted items may be ``str`` (returned) or ``Exception`` (raised), which lets tests
    exercise invalid JSON, schema violations and provider failures.
    """

    name = "mock"

    def __init__(self, script: Iterable[str | Exception] = (), responder: Responder | None = None):
        self._script = deque(script)
        self._responder = responder
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._script:
            item = self._script.popleft()
            if isinstance(item, Exception):
                raise item
            text = item
        elif self._responder is not None:
            text = self._responder(request)
        else:
            raise LLMError("MockProvider script exhausted")
        prompt_chars = sum(len(m.content) for m in request.messages)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=request.model or "mock-1",
            usage=Usage(input_tokens=prompt_chars // 4, output_tokens=len(text) // 4),
        )
