"""Anthropic Messages API adapter (hosted provider, ``external = True``).

Uses plain HTTPS through ``httpx`` (install the ``anthropic`` extra) so the vendor SDK is not a
dependency. Tested against a mocked transport only; it has not been exercised against the live
API in this repository's CI.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import LLMError, LLMRequest, LLMResponse, LLMTimeout, Usage

DEFAULT_MODEL = "claude-sonnet-5-5"
API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"
    external = True

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = "https://api.anthropic.com",
        client: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required")
        import httpx  # imported lazily: optional dependency

        self._httpx = httpx
        self.model = model
        self._client = client or httpx.Client(base_url=base_url)
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}

    def complete(self, request: LLMRequest) -> LLMResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        if request.response_schema is not None:
            system_parts.append(
                "Respond with a single JSON document that validates against this JSON Schema, "
                "and nothing else:\n" + json.dumps(request.response_schema, separators=(",", ":"))
            )
        turns = []
        for m in request.messages:
            if m.role == "system":
                continue
            if m.role not in ("user", "assistant"):
                raise LLMError(f"unsupported message role {m.role!r}")
            turns.append({"role": m.role, "content": m.content})
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": turns,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        started = time.perf_counter()
        try:
            resp = self._client.post("/v1/messages", json=payload, headers=self._headers, timeout=request.timeout_s)
        except self._httpx.TimeoutException as e:
            raise LLMTimeout("request to the model provider timed out") from e
        except self._httpx.HTTPError as e:
            raise LLMError(f"could not reach the model provider ({type(e).__name__})") from e

        if resp.status_code != 200:
            raise LLMError(f"model provider returned HTTP {resp.status_code}: {_error_message(resp)}")
        try:
            body = resp.json()
            text = "".join(b.get("text", "") for b in body["content"] if b.get("type") == "text")
            usage = body.get("usage", {})
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError("unexpected response from the model provider") from e
        return LLMResponse(
            text=text,
            provider=self.name,
            model=body.get("model", payload["model"]),
            # Pricing is deployment-specific and changes; cost is left at 0 rather than guessed.
            usage=Usage(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), 0.0),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=str(body.get("stop_reason") or "stop"),
        )


def _error_message(resp: Any) -> str:
    try:
        return str(resp.json()["error"]["message"])[:200]
    except (ValueError, KeyError, TypeError):
        return "no details"
