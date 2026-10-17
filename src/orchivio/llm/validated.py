"""Schema-validated generation with bounded, safe retries (DESIGN.md Phase 4).

Invalid output is retried by showing the model its validation errors, never by executing
or trusting what it returned. After ``max_attempts`` the call fails with
``StructuredOutputError`` carrying every attempt for the audit trail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from .base import LLMProvider, LLMRequest, LLMResponse, Message, Usage

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class StructuredOutputError(Exception):
    def __init__(self, message: str, attempts: list[LLMResponse], errors: list[str]) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.errors = errors


@dataclass(frozen=True)
class Validated(Generic[T]):
    value: T
    attempts: int
    usage: Usage
    last_response: LLMResponse


def _extract_json(text: str) -> object:
    body = text.strip()
    m = _FENCE_RE.match(body)
    if m:
        body = m.group(1)
    return json.loads(body)


def generate_validated(
    provider: LLMProvider,
    request: LLMRequest,
    schema: type[T],
    *,
    max_attempts: int = 3,
) -> Validated[T]:
    """Call ``provider`` until its output validates against ``schema``."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    req = replace(request, response_schema=request.response_schema or schema.model_json_schema())
    attempts: list[LLMResponse] = []
    errors: list[str] = []
    total = Usage()
    for _ in range(max_attempts):
        resp = provider.complete(req)  # provider errors propagate: not a validation problem
        attempts.append(resp)
        total = total + resp.usage
        try:
            value = schema.model_validate(_extract_json(resp.text))
            return Validated(value, len(attempts), total, resp)
        except (json.JSONDecodeError, ValidationError) as e:
            msg = _summarise(e)
            errors.append(msg)
            req = replace(
                req,
                messages=(
                    *request.messages,
                    Message("assistant", resp.text[:2000]),
                    Message(
                        "user",
                        "Your previous answer was invalid. Fix these problems and answer with "
                        f"JSON only:\n{msg}",
                    ),
                ),
            )
    raise StructuredOutputError(
        f"no valid output after {max_attempts} attempt(s): {errors[-1]}", attempts, errors
    )


def _summarise(e: json.JSONDecodeError | ValidationError) -> str:
    if isinstance(e, json.JSONDecodeError):
        return f"not valid JSON ({e.msg} at position {e.pos})"
    return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:10])
