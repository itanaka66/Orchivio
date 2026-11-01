"""Anna AI OS Sampling API adapter (internal provider, ``external = False``).

Routes LLM completions through the Anna host's sampling API via the executa
plugin's local relay server. The relay URL is configured via
``ORCHIVIO_ANNA_SAMPLING_URL`` (e.g. ``http://127.0.0.1:18765``).

Prompts never leave the Anna AI OS deployment; billing and model selection are
handled by the host.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import LLMError, LLMRequest, LLMResponse, LLMTimeout, Usage

DEFAULT_MODEL = "anna-default"


class AnnaSamplingProvider:
    name = "anna"
    external = False  # prompts stay within the Anna AI OS deployment

    def __init__(self, relay_url: str, *, client: Any = None) -> None:
        import httpx

        self._httpx = httpx
        self._relay_url = relay_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self._relay_url)

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, str]] = []
        system_parts: list[str] = []
        for m in request.messages:
            if m.role == "system":
                system_parts.append(m.content)
            elif m.role in ("user", "assistant"):
                messages.append({"role": m.role, "content": m.content})
            else:
                raise LLMError(f"unsupported message role {m.role!r}")

        if request.response_schema is not None:
            system_parts.append(
                "Respond with a single JSON document that validates against this JSON Schema, "
                "and nothing else:\n" + json.dumps(request.response_schema, separators=(",", ":"))
            )

        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.model:
            payload["model"] = request.model

        started = time.perf_counter()
        try:
            resp = self._client.post("/complete", json=payload, timeout=request.timeout_s)
        except self._httpx.TimeoutException as e:
            raise LLMTimeout("Anna sampling relay timed out") from e
        except self._httpx.HTTPError as e:
            raise LLMError(f"could not reach Anna sampling relay ({type(e).__name__})") from e

        if resp.status_code != 200:
            raise LLMError(f"Anna sampling relay returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
            text = body["text"]
        except (ValueError, KeyError, TypeError) as e:
            raise LLMError("unexpected response from Anna sampling relay") from e

        model_id = body.get("model", DEFAULT_MODEL)
        in_tok = int(body.get("input_tokens", 0))
        out_tok = int(body.get("output_tokens", 0))
        return LLMResponse(
            text=text,
            provider=self.name,
            model=model_id,
            usage=Usage(in_tok, out_tok, 0.0),  # billing is handled by Anna
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=body.get("stop_reason", "end_turn"),
        )
