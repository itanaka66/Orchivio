"""The real adapter for consumed REST APIs (FR-081): resolve a logical destination, authenticate, call, parse.

All network access goes through the pinned, SSRF-checked transport (the same one webhooks use), so a destination
configured to point at an internal address is refused at call time, redirects are never followed, and responses are
size-limited. Credentials are read from the secret reference at call time and only ever placed in a header.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchivio import telemetry
from orchivio.db.models import IntegrationDestination
from orchivio.ports import IntegrationCall, IntegrationError, IntegrationResult

from .dispatcher import SecretUnavailable, resolve_secret
from .ssrf import DestinationError
from .transport import Response

MAX_RESPONSE_BYTES = 1024 * 1024


class RequestTransport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None, *,
                timeout: float | None = None, max_bytes: int = ...) -> Response: ...


class RestClient:
    """An ``IntegrationClient`` bound to one application."""

    def __init__(self, application_id: UUID, sessions: sessionmaker[Session], transport: RequestTransport, *,
                 secrets: Callable[[str], str] = resolve_secret) -> None:
        self.application_id, self.sessions, self.transport, self.secrets = application_id, sessions, transport, secrets

    def _destination(self, name: str) -> IntegrationDestination:
        with self.sessions() as s:
            row = s.scalar(select(IntegrationDestination).where(IntegrationDestination.application_id == self.application_id,
                                                                IntegrationDestination.name == name))
            if row is None:
                raise IntegrationError(f"destination '{name}' is not configured for this application")
            s.expunge(row)
            return row

    def call(self, call: IntegrationCall) -> IntegrationResult:
        started = time.perf_counter()
        with telemetry.span("integration.call", kind="client", **{"integration": call.integration, "operation": call.operation,
                                                               "http.request.method": call.method}) as sp:
            try:
                out = self._call(call)
            except IntegrationError:
                telemetry.count("orchivio.integration.calls", status_class="error")
                raise
            klass = f"{out.status // 100}xx"
            sp.set_attribute("http.response.status_code", out.status)
            if out.status >= 500:
                sp.set_error(f"HTTP {out.status}")
        telemetry.count("orchivio.integration.calls", status_class=klass)
        telemetry.record("orchivio.integration.call.duration", time.perf_counter() - started, status_class=klass)
        return out

    def _call(self, call: IntegrationCall) -> IntegrationResult:
        dest = self._destination(call.destination)
        url = dest.base_url.rstrip("/") + call.path + (("?" + urlencode(call.query)) if call.query else "")
        headers = {"Accept": "application/json", "User-Agent": "Orchivio-Integrations/1"}
        if dest.auth_type != "none":
            try:
                secret = self.secrets(dest.secret_ref or "")
            except SecretUnavailable as e:
                raise IntegrationError(f"the credential for destination '{dest.name}' is not available") from e
            if dest.auth_type == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                headers[dest.auth_header or "X-Api-Key"] = secret
        body = None
        if call.body is not None:
            body = json.dumps(call.body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            resp = self.transport.request(call.method, url, headers, body, timeout=call.timeout_seconds, max_bytes=MAX_RESPONSE_BYTES)
        except DestinationError as e:
            raise IntegrationError(f"destination refused: {e}") from e
        except OSError as e:
            raise IntegrationError(f"network error ({type(e).__name__})") from e
        return IntegrationResult(resp.status, _json(resp))


def _json(resp: Response) -> Any:
    if not resp.body:
        return None
    if len(resp.body) >= MAX_RESPONSE_BYTES:
        raise IntegrationError("the response is larger than the 1 MiB limit")
    try:
        return json.loads(resp.body)
    except ValueError:
        return None  # not JSON: the engine reports it if a typed response was expected
