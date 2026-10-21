"""OpenAI-compatible API adapter for locally-hosted models (Ollama, llama.cpp, LM Studio, etc.).

``external = False``: prompts never leave the local machine when using the default base URL.
Operators may point ``base_url`` at a remote OpenAI-compatible endpoint and set ``external=True``
explicitly, but the default is safe for air-gapped / privacy-sensitive deployments.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import LLMError, LLMRequest, LLMResponse, LLMTimeout, Usage

DEFAULT_MODEL = "llama3"
DEFAULT_BASE_URL = "http://localhost:11434"  # Ollama default


class OllamaProvider:
    """Local LLM via any OpenAI-compatible ``/v1/chat/completions`` endpoint.

    Works with Ollama, llama.cpp server, LM Studio, Jan, and anything else that
    speaks the OpenAI Chat Completions protocol.
    """

    name = "ollama"
    external = False  # prompts stay on-machine with the default base_url

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "ollama",  # most local servers ignore the key; pass "" to omit
        client: Any = None,
        external: bool | None = None,  # override when pointing at a remote endpoint
    ) -> None:
        import httpx  # lazy: optional dependency

        self._httpx = httpx
        self.model = model
        if external is not None:
            self.external = external
        headers: dict[str, str] = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.Client(base_url=base_url)
        self._headers = headers

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, str]] = []
        for m in request.messages:
            if m.role not in ("system", "user", "assistant"):
                raise LLMError(f"unsupported message role {m.role!r}")
            messages.append({"role": m.role, "content": m.content})

        if request.response_schema is not None:
            # Inject the schema instruction as a system message before user turns.
            schema_instruction = (
                "Respond with a single JSON document that validates against this JSON Schema, "
                "and nothing else:\n" + json.dumps(request.response_schema, separators=(",", ":"))
            )
            # Prepend if no system message exists; append to the last system block otherwise.
            if messages and messages[0]["role"] == "system":
                messages[0] = {"role": "system", "content": messages[0]["content"] + "\n\n" + schema_instruction}
            else:
                messages.insert(0, {"role": "system", "content": schema_instruction})

        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }

        started = time.perf_counter()
        try:
            resp = self._client.post(
                "/v1/chat/completions",
                json=payload,
                headers=self._headers,
                timeout=request.timeout_s,
            )
        except self._httpx.TimeoutException as e:
            raise LLMTimeout("local model timed out") from e
        except self._httpx.HTTPError as e:
            raise LLMError(f"could not reach local model server ({type(e).__name__})") from e

        if resp.status_code != 200:
            raise LLMError(f"local model server returned HTTP {resp.status_code}: {_error_message(resp)}")

        try:
            body = resp.json()
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
            finish_reason = str(choice.get("finish_reason") or "stop")
            usage = body.get("usage", {})
        except (ValueError, KeyError, TypeError, IndexError) as e:
            raise LLMError("unexpected response from local model server") from e

        model_id = body.get("model", payload["model"])
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        return LLMResponse(
            text=text,
            provider=self.name,
            model=model_id,
            usage=Usage(in_tok, out_tok, 0.0),  # local models have no API cost
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=finish_reason,
        )


def _error_message(resp: Any) -> str:
    try:
        body = resp.json()
        return str(body.get("error", {}).get("message") or body)[:200]
    except (ValueError, KeyError, TypeError):
        return "no details"
